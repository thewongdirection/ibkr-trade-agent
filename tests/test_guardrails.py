"""Tests for the risk layer and the paper/live interlock."""

import os

import pytest

from agent.settings import RiskLimits, Settings, load_settings
from risk.guardrails import (
    OrderProposal,
    PortfolioState,
    check_order_instruction,
    evaluate_proposal,
    filter_new_entries,
)


def make_settings(**overrides) -> Settings:
    risk = RiskLimits(
        max_order_notional=5000,
        max_position_weight_pct=15,
        max_sector_weight_pct=35,
        max_new_positions_per_review=3,
        cash_buffer_pct=5,
        stop_loss_pct=8,
        correction_stop_loss_pct=3,
        take_profit_pct=22,
    )
    base = dict(
        mode="paper", config_mode="paper", base_currency="USD", account_verify={},
        strategy_style="blend",
        asset_classes=("stock", "etf", "option"), new_ideas_count=20, risk=risk,
        management={"hard_exit_on_loss_pct": 8, "trim_on_gain_pct": 22, "option_dte_warn": 10},
        universe={}, schedule={}, recommend_skill_path="skills/can-slim-recommend",
        grader_skill_path="skills/can-slim-grader", journal_db_path="journal/x.db",
    )
    base.update(overrides)
    return Settings(**base)


def portfolio(equity=100_000, cash=50_000, positions=None, sectors=None) -> PortfolioState:
    return PortfolioState(
        equity=equity, cash=cash,
        position_values=positions or {}, sector_values=sectors or {},
    )


# --- paper/live interlock ---------------------------------------------------

def test_config_live_without_env_stays_paper(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "account:\n  mode: live\n"
        "risk:\n  max_order_notional_usd: 5000\n  max_position_weight_pct: 15\n"
        "  max_sector_weight_pct: 35\n"
    )
    monkeypatch.delenv("IBKR_ALLOW_LIVE", raising=False)
    s = load_settings(cfg)
    assert s.mode == "paper"
    assert s.config_mode == "live"  # remembers what config asked for
    assert not s.is_live


def test_config_live_with_env_goes_live(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "account:\n  mode: live\n"
        "risk:\n  max_order_notional_usd: 5000\n  max_position_weight_pct: 15\n"
        "  max_sector_weight_pct: 35\n"
    )
    monkeypatch.setenv("IBKR_ALLOW_LIVE", "1")
    assert load_settings(cfg).mode == "live"


def test_env_without_config_stays_paper(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "account:\n  mode: paper\n"
        "risk:\n  max_order_notional_usd: 5000\n  max_position_weight_pct: 15\n"
        "  max_sector_weight_pct: 35\n"
    )
    monkeypatch.setenv("IBKR_ALLOW_LIVE", "1")
    assert load_settings(cfg).mode == "paper"


# --- per-order notional cap -------------------------------------------------

def test_notional_cap_blocks_oversized_buy():
    s = make_settings()
    p = OrderProposal("NVDA", "BUY", quantity=100, limit_price=100, asset_class="stock",
                      sector="Tech", stop_price=92)
    v = evaluate_proposal(p, portfolio(cash=50_000), s)
    assert not v.ok
    assert "per-order cap" in v.reason


def test_option_notional_uses_100x_multiplier():
    s = make_settings()
    # 1 contract * $60 * 100 = $6,000 > $5,000 cap
    p = OrderProposal("AAPL", "BUY", quantity=1, limit_price=60, asset_class="option",
                      sector="Tech", stop_price=55)
    assert not evaluate_proposal(p, portfolio(), s).ok


# --- stop-loss requirement --------------------------------------------------

def test_buy_without_stop_is_rejected():
    s = make_settings()
    p = OrderProposal("MSFT", "BUY", quantity=10, limit_price=100, asset_class="stock",
                      sector="Tech", stop_price=None)
    v = evaluate_proposal(p, portfolio(), s)
    assert not v.ok
    assert "stop-loss" in v.reason


# --- weight + sector + cash caps -------------------------------------------

def test_position_weight_cap():
    s = make_settings()
    # existing $12k + new $4k = $16k on $100k = 16% > 15% cap
    p = OrderProposal("TSLA", "BUY", quantity=40, limit_price=100, asset_class="stock",
                      sector="Auto", stop_price=92)
    v = evaluate_proposal(p, portfolio(positions={"TSLA": 12_000}), s)
    assert not v.ok
    assert "of equity" in v.reason


def test_sector_weight_cap():
    s = make_settings()
    p = OrderProposal("AMD", "BUY", quantity=40, limit_price=100, asset_class="stock",
                      sector="Tech", stop_price=92)
    v = evaluate_proposal(p, portfolio(sectors={"Tech": 33_000}), s)
    assert not v.ok
    assert "sector" in v.reason


def test_cash_buffer_cap():
    s = make_settings()
    # cash 4000, buffer 5% of 100k = 5000; any buy breaches
    p = OrderProposal("KO", "BUY", quantity=10, limit_price=100, asset_class="stock",
                      sector="Staples", stop_price=92)
    v = evaluate_proposal(p, portfolio(cash=4_000), s)
    assert not v.ok
    assert "cash buffer" in v.reason


def test_clean_buy_passes():
    s = make_settings()
    p = OrderProposal("PANW", "BUY", quantity=10, limit_price=100, asset_class="stock",
                      sector="Tech", stop_price=92)
    v = evaluate_proposal(p, portfolio(), s)
    assert v.ok, v.reason


def test_sell_bypasses_buy_caps():
    s = make_settings()
    # A big sell would breach notional if treated as a buy, but sells reduce risk.
    p = OrderProposal("NVDA", "SELL", quantity=1000, limit_price=100, asset_class="stock",
                      sector="Tech")
    assert evaluate_proposal(p, portfolio(), s).ok


# --- max new positions cap --------------------------------------------------

def test_max_new_positions_cap():
    s = make_settings()  # cap = 3
    props = [
        OrderProposal(sym, "BUY", quantity=10, limit_price=100, asset_class="stock",
                      sector=f"S{i}", stop_price=92)
        for i, sym in enumerate(["A", "B", "C", "D", "E"])
    ]
    accepted, rejected = filter_new_entries(props, portfolio(cash=90_000), s)
    assert len(accepted) == 3
    assert all("max new positions" in v.reason for _, v in rejected)


# --- raw MCP payload defensive check ---------------------------------------

def test_raw_payload_blocks_unpriced_buy():
    s = make_settings()
    v = check_order_instruction({"side": "BUY", "quantity": 10}, s)
    assert not v.ok


def test_raw_payload_blocks_disabled_asset_class():
    s = make_settings(asset_classes=("stock",))
    v = check_order_instruction(
        {"side": "BUY", "asset_class": "future", "quantity": 1, "price": 100}, s
    )
    assert not v.ok


def test_raw_payload_allows_sane_buy():
    s = make_settings()
    v = check_order_instruction(
        {"side": "BUY", "asset_class": "stock", "quantity": 10, "price": 100}, s
    )
    assert v.ok
