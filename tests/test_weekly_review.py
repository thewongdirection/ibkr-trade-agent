"""End-to-end test of the weekly review orchestration with injected fixtures.

Exercises the full run_review flow — market read, position grading + management decisions,
new-idea sizing, risk filtering, staging, and journaling — without any live connector, by
injecting a ReviewContext backed by fixtures. Also serves as the runnable example of how the
Agent-SDK-backed context will be wired.
"""

import pytest

from agent.settings import RiskLimits, Settings
from analysis.canslim import Grade, Recommendation, Verdict
from journal.store import Journal
from workflows.weekly_review import ReviewContext, format_brief, run_review


@pytest.fixture
def settings(tmp_path):
    # Point skill paths at fake dirs containing a SKILL.md so require_skills passes.
    for name in ("can-slim-recommend", "can-slim-grader"):
        d = tmp_path / name
        d.mkdir()
        (d / "SKILL.md").write_text("stub")
    risk = RiskLimits(5000, 15, 35, 3, 5, 8, 3, 22)
    return Settings(
        mode="paper", config_mode="paper", base_currency="USD", strategy_style="blend",
        asset_classes=("stock", "etf", "option"), new_ideas_count=5, risk=risk,
        management={"hard_exit_on_loss_pct": 8, "trim_on_gain_pct": 22, "option_dte_warn": 10},
        universe={}, schedule={},
        recommend_skill_path=tmp_path / "can-slim-recommend",
        grader_skill_path=tmp_path / "can-slim-grader",
        journal_db_path=tmp_path / "journal.db",
    )


@pytest.fixture
def context():
    positions = [
        # A loser through the stop -> EXIT
        {"symbol": "LOSER", "position": 100, "market_value": 8_000, "avg_cost": 100,
         "unrealized_pnl_pct": -12, "sector": "Tech", "asset_class": "stock"},
        # A big winner -> TRIM
        {"symbol": "WIN", "position": 50, "market_value": 10_000, "avg_cost": 130,
         "unrealized_pnl_pct": 30, "sector": "Health", "asset_class": "stock"},
        # A healthy hold
        {"symbol": "HOLD", "position": 20, "market_value": 5_000, "avg_cost": 240,
         "unrealized_pnl_pct": 4, "sector": "Energy", "asset_class": "stock"},
    ]
    balances = {"net_liquidation": 100_000, "cash": 60_000}

    grades = {
        "LOSER": Grade("LOSER", Verdict.AVOID, 30, {"C": 3}, summary="deteriorated"),
        "WIN": Grade("WIN", Verdict.BUY_RANGE, 62, {"C": 9}, summary="leader"),
        "HOLD": Grade("HOLD", Verdict.WATCH, 55, {"C": 7}, summary="repairing base"),
    }
    recs = [
        Recommendation("PANW", "Palo Alto", "Security", 64, price=180, buy_point=182,
                       stop=168, rs=90, reason="cup-with-handle breakout on volume"),
        Recommendation("ANET", "Arista", "Networking", 61, price=95, buy_point=96,
                       stop=88, rs=88, reason="new high off flat base, RS leader"),
    ]

    staged: list = []
    return ReviewContext(
        fetch_positions=lambda: positions,
        fetch_balances=lambda: balances,
        fetch_sectors=lambda syms: {},
        assess_market=lambda: "Confirmed uptrend",
        grade_symbol=lambda sym: grades[sym],
        recommend_new=lambda count: recs,
        stage_order=lambda p: staged.append(p),
    ), staged


def test_full_review_runs_and_journals(settings, context):
    ctx, staged = context
    result = run_review(settings, ctx, stage=True)

    # Management decisions: LOSER exits, WIN trims, HOLD holds.
    actions = {m["symbol"]: m["action"] for m in result.management}
    assert actions == {"LOSER": "EXIT", "WIN": "TRIM", "HOLD": "HOLD"}

    # New entries were sized, risk-checked, and staged (plus the exit + trim sells).
    staged_symbols = {s["symbol"] for s in result.staged}
    assert {"LOSER", "WIN"}.issubset(staged_symbols)      # sells
    assert staged_symbols & {"PANW", "ANET"}              # at least one buy survived caps

    # Every buy that was staged carries a stop.
    for s in result.staged:
        if s["side"] == "BUY":
            assert s["stop"] is not None

    # Staging actually invoked the connector hook.
    assert len(staged) == len(result.staged)

    # Journal persisted the run + decisions.
    journal = Journal(settings.journal_db_path)
    decisions = journal.decisions_for_run(result.run_id)
    assert decisions
    assert journal.latest_grade("WIN")["verdict"] == "BUY-RANGE"


def test_dry_run_stages_nothing(settings, context):
    ctx, staged = context
    result = run_review(settings, ctx, stage=False)
    assert staged == []                      # connector never called
    assert all(s for s in result.staged)     # still computed proposals
    brief = format_brief(result, settings)
    assert "PROPOSED (not staged)" in brief


def test_missing_skills_raises(settings, context):
    ctx, _ = context
    # Break the skill path.
    object.__setattr__(settings, "grader_skill_path", settings.grader_skill_path / "nope")
    with pytest.raises(FileNotFoundError):
        run_review(settings, ctx, stage=False)
