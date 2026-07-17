"""Tests for the broker layer, seeded with responses captured from the live IBKR connector.

The fixtures below are the exact shapes the IBKR MCP tools returned, so parsing is validated
against reality rather than assumptions. The account was empty at capture time, so the
positions/trades/orders parsers are additionally exercised with synthetic rows shaped per the
connector tool descriptions.
"""

import pytest

from agent.settings import RiskLimits, Settings
from broker.client import (
    StaticBrokerClient,
    parse_account_meta,
    parse_account_summary,
    parse_orders,
    parse_positions,
    parse_trades,
)
from broker.mode import set_config_mode, status_report
from broker.session import verify_connection

# --- real captured shapes ---------------------------------------------------

REAL_SUMMARY = {
    "currency": "SGD", "net_liquidation": 0, "equity_with_loan_value": 0, "buying_power": 0,
    "gross_position_value": 0, "total_cash_value": 0, "available_funds": 0,
    "initial_margin": 0, "maintenance_margin": 0, "excess_liquidity": 0, "dividends": 0,
    "leverage": "0.0",
}
REAL_BALANCES = {
    "balances": [
        {"currency": "BASE", "cash_balance": 0, "settled_cash": 0, "net_liquidation_value": 0,
         "stock_market_value": 0, "unrealized_pnl": 0, "realized_pnl": 0, "exchange_rate": 1},
        {"currency": "SGD", "cash_balance": 0, "settled_cash": 0, "net_liquidation_value": 0,
         "stock_market_value": 0, "unrealized_pnl": 0, "realized_pnl": 0, "exchange_rate": 1},
    ]
}
REAL_PERFORMANCE = {
    "portfolio_measure": "TWR", "currency_type": "base",
    "accounts": {"account": {
        "base_currency": "SGD", "start": "20260716", "end": "20260716",
        "last_successful_update": "2026-07-17 00:33:39",
        "available_periods": ["1D", "7D", "MTD", "1M", "YTD", "1Y"], "periods": {},
    }},
}


def make_settings(base_currency="SGD", verify=None, **overrides) -> Settings:
    risk = RiskLimits(5000, 15, 35, 3, 5, 8, 3, 22)
    base = dict(
        mode="paper", config_mode="paper", base_currency=base_currency,
        account_verify=verify or {}, strategy_style="blend",
        asset_classes=("stock", "etf", "option"), new_ideas_count=20, risk=risk,
        management={}, universe={}, schedule={},
        recommend_skill_path="skills/can-slim-recommend",
        grader_skill_path="skills/can-slim-grader", journal_db_path="journal/x.db",
    )
    base.update(overrides)
    return Settings(**base)


# --- summary / balances / meta parsing --------------------------------------

def test_parse_real_summary():
    s = parse_account_summary(REAL_SUMMARY)
    assert s.currency == "SGD"
    assert s.net_liquidation == 0
    assert s.leverage == 0.0        # coerced from the "0.0" string
    assert not s.is_funded


def test_parse_real_balances_has_base_and_sgd():
    client = StaticBrokerClient(balances=REAL_BALANCES)
    bals = client.balances()
    assert {b.currency for b in bals} == {"BASE", "SGD"}
    assert all(b.exchange_rate == 1 for b in bals)


def test_parse_real_account_meta():
    m = parse_account_meta(REAL_PERFORMANCE)
    assert m.base_currency == "SGD"
    assert m.inception_date == "20260716"
    assert m.last_update == "2026-07-17 00:33:39"
    assert m.account_key == "account"   # connector masks the real id behind this key


# --- positions / trades / orders (synthetic, per tool docs) -----------------

def test_parse_positions_synthetic():
    data = {"positions": [
        {"symbol": "NVDA", "position": 100, "price": 120.0, "market_value": 12000.0,
         "avg_cost": 100.0, "unrealized_pnl": 2000.0, "sec_type": "STK", "currency": "USD"},
    ]}
    pos = parse_positions(data)[0]
    assert pos.symbol == "NVDA"
    assert pos.quantity == 100
    assert pos.asset_class == "stk"
    assert round(pos.unrealized_pnl_pct, 1) == 20.0


def test_parse_trades_synthetic():
    data = {"trades": [
        {"trade_id": "T1", "symbol": "AAPL", "side": "BUY", "size": 10, "price": 200.0,
         "commission": 1.0, "trade_time": "2026-07-01 14:30:00", "currency": "USD"},
    ]}
    t = parse_trades(data)[0]
    assert t.trade_id == "T1" and t.symbol == "AAPL" and t.side == "BUY"
    assert t.size == 10 and t.commission == 1.0


def test_parse_orders_synthetic():
    data = {"orders": [
        {"order_id": "O1", "symbol": "MSFT", "side": "SELL", "order_type": "LMT",
         "status": "Submitted", "quantity": 5, "price": 400.0, "filled": 0},
    ]}
    o = parse_orders(data)[0]
    assert o.order_id == "O1" and o.status == "Submitted" and o.order_type == "LMT"


# --- connection + identity verification -------------------------------------

def live_client():
    return StaticBrokerClient(
        summary=REAL_SUMMARY, balances=REAL_BALANCES, performance=REAL_PERFORMANCE,
    )


def test_verify_connection_connected_but_unverified():
    # base_currency matches so no fx warning; identity not configured -> None.
    status = verify_connection(live_client(), make_settings(base_currency="SGD"))
    assert status.connected
    assert status.base_currency == "SGD"
    assert status.identity_verified is None
    assert status.ok_to_trade                      # unconfigured != failing
    assert status.fingerprint["account_inception"] == "20260716"
    assert status.fingerprint["account_number"].startswith("masked")


def test_verify_connection_identity_match():
    s = make_settings(base_currency="SGD", verify={"expected_base_currency": "SGD"})
    status = verify_connection(live_client(), s)
    assert status.identity_verified is True
    assert status.ok_to_trade


def test_verify_connection_identity_mismatch_blocks_trading():
    s = make_settings(base_currency="USD", verify={"expected_base_currency": "USD",
                                                   "label": "my-usd-acct"})
    status = verify_connection(live_client(), s)
    assert status.identity_verified is False
    assert not status.ok_to_trade
    assert any("IDENTITY MISMATCH" in w for w in status.warnings)


def test_currency_mismatch_warns():
    # config says USD, live account is SGD -> warn that caps are read in SGD.
    s = make_settings(base_currency="USD")
    status = verify_connection(live_client(), s)
    assert any("interpreted in SGD" in w for w in status.warnings)


# --- mode switching ---------------------------------------------------------

CONFIG_TEMPLATE = (
    "account:\n"
    "  mode: paper           # comment preserved\n"
    "  base_currency: USD\n"
    "risk:\n"
    "  max_order_notional_usd: 5000\n"
    "  max_position_weight_pct: 15\n"
    "  max_sector_weight_pct: 35\n"
)


def test_set_config_mode_roundtrip_preserves_comments(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(CONFIG_TEMPLATE)

    prev = set_config_mode("live", cfg)
    assert prev == "paper"
    text = cfg.read_text()
    assert "mode: live" in text
    assert "# comment preserved" in text          # targeted edit kept the comment

    prev = set_config_mode("paper", cfg)
    assert prev == "live"
    assert "mode: paper" in cfg.read_text()


def test_set_config_mode_rejects_bad_value(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(CONFIG_TEMPLATE)
    with pytest.raises(ValueError):
        set_config_mode("margin", cfg)


def test_live_config_without_env_stays_paper(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(CONFIG_TEMPLATE)
    set_config_mode("live", cfg)
    monkeypatch.delenv("IBKR_ALLOW_LIVE", raising=False)
    report = status_report(cfg)
    assert "EFFECTIVE mode      : PAPER" in report
    assert "still PAPER" in report
