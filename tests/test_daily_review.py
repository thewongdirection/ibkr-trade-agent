"""End-to-end test of the daily review + dashboard + delivery, with injected fixtures.

Covers both idea sources (CAN SLIM screen + monitor signal feed), grading, risk filtering,
staging, the dashboard render, the chat brief, and the push payload — no live connector.
"""

from datetime import datetime, timezone

import pytest

from agent.settings import RiskLimits, Settings
from analysis.canslim import Grade, Recommendation, Verdict
from journal.store import Journal
from signals.feed import load_feed
from workflows.daily_review import (
    DailyContext,
    build_dashboard,
    chat_brief,
    push_payload,
    run_daily_review,
)

NOW = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def settings(tmp_path):
    for name in ("can-slim-recommend", "can-slim-grader"):
        d = tmp_path / name
        d.mkdir()
        (d / "SKILL.md").write_text("stub")
    risk = RiskLimits(40000, 15, 35, 3, 5, 8, 3, 22)
    return Settings(
        mode="paper", config_mode="paper", base_currency="SGD", account_verify={},
        strategy_style="blend", asset_classes=("stock", "etf", "option"), new_ideas_count=5,
        risk=risk,
        management={"hard_exit_on_loss_pct": 8, "trim_on_gain_pct": 22, "option_dte_warn": 10},
        universe={}, schedule={},
        recommend_skill_path=tmp_path / "can-slim-recommend",
        grader_skill_path=tmp_path / "can-slim-grader",
        journal_db_path=tmp_path / "journal.db",
    )


def _funded_context(**overrides):
    positions = [
        {"symbol": "LOSER", "position": 100, "market_value": 8000, "avg_cost": 100,
         "unrealized_pnl_pct": -12, "sector": "Tech", "asset_class": "stock"},
    ]
    balances = {"net_liquidation": 250000, "cash": 250000}
    grades = {
        "LOSER": Grade("LOSER", Verdict.AVOID, 30, {"C": 3}, summary="deteriorated"),
        "PANW": Grade("PANW", Verdict.BUY_RANGE, 64, {"C": 9}, pivot=182, stop=168, summary="leader"),
        "NVDA": Grade("NVDA", Verdict.BUY_RANGE, 66, {"C": 9}, pivot=120, stop=110, summary="signal leader"),
    }
    recs = [Recommendation("PANW", "Palo Alto", "Security", 64, price=180, buy_point=182,
                           stop=168, rs=90, reason="cup-with-handle breakout")]
    feed = load_feed({"signals": [
        {"ticker": "NVDA", "detector": "insider_trades", "severity": "high",
         "headline": "Director purchase — cluster buy", "occurred_at": "2026-07-27T18:00:00Z"},
        {"ticker": "NVDA", "detector": "option_volume", "severity": "medium",
         "headline": "call vol 3x", "occurred_at": "2026-07-27T15:00:00Z"},
    ]})
    staged: list = []
    ctx = DailyContext(
        fetch_positions=lambda: positions,
        fetch_balances=lambda: balances,
        assess_market=lambda: "Confirmed uptrend",
        grade_symbol=lambda s: grades[s],
        recommend_new=lambda n: recs,
        load_signals=lambda: feed,
        identity_verified=True,
        stage_order=lambda p: staged.append(p),
        now=NOW,
    )
    for k, v in overrides.items():
        setattr(ctx, k, v)
    return ctx, staged


def test_daily_review_both_sources(settings):
    ctx, staged = _funded_context()
    result = run_daily_review(settings, ctx, stage=True)

    # Holding graded AVOID -> EXIT.
    assert result.management[0]["action"] == "EXIT"

    staged_syms = {p["symbol"] for p in result.proposals}
    assert "LOSER" in staged_syms                       # the exit sell
    assert "PANW" in staged_syms                        # from CAN SLIM screen
    assert "NVDA" in staged_syms                        # from the monitor signal feed
    assert len(staged) == len(result.proposals)

    # NVDA appears as a graded signal candidate too.
    assert any(c["ticker"] == "NVDA" for c in result.signal_candidates)

    # Every buy carries a stop.
    for p in result.proposals:
        if p["side"] == "BUY":
            assert p["stop"] is not None

    # Journaled.
    j = Journal(settings.journal_db_path)
    assert j.decisions_for_run(result.run_id)


def test_dashboard_renders_self_contained(settings):
    ctx, _ = _funded_context()
    result = run_daily_review(settings, ctx, stage=True)
    html = build_dashboard(result, settings, "2026-07-28")
    assert html.startswith("<!doctype html>")
    assert "PANW" in html and "NVDA" in html
    assert "http" not in html.split("</style>")[0].replace("https://json", "")  # no external URLs in head
    assert "account number &amp; owner masked" in html
    # Money shown in account currency.
    assert "SGD" in html


def test_identity_mismatch_blocks_orders(settings):
    ctx, staged = _funded_context(identity_verified=False)
    result = run_daily_review(settings, ctx, stage=True)
    assert result.proposals == []
    assert staged == []
    assert any("MISMATCH" in n for n in result.notes)
    push = push_payload(result)
    assert "halted" in push["title"].lower()


def test_push_and_brief(settings):
    ctx, _ = _funded_context()
    result = run_daily_review(settings, ctx, stage=True)
    push = push_payload(result)
    assert "approve" in push["title"].lower()
    brief = chat_brief(result, settings)
    assert "Daily review" in brief and "PANW" in brief


def test_unfunded_account_notes(settings):
    ctx, _ = _funded_context(
        fetch_positions=lambda: [], fetch_balances=lambda: {"net_liquidation": 0, "cash": 0},
        recommend_new=lambda n: [], load_signals=lambda: [])
    result = run_daily_review(settings, ctx, stage=False)
    assert any("unfunded" in n.lower() for n in result.notes)
