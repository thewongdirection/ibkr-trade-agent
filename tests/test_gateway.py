"""Tests for the self-hosted IB Gateway transport (broker/gateway.py).

Everything runs against a fake ``IB`` object shaped like ib_async's, so the suite needs no
gateway, no network, and not even ib_async installed. The safety-critical assertions are the
mode/port interlock and that staged orders are never transmitted.
"""

from __future__ import annotations

import pytest

from agent.settings import RiskLimits, Settings
from broker.gateway import (
    GatewayConfig,
    GatewayError,
    GatewayOrderStager,
    IBGatewayBrokerClient,
    check_mode_port,
    daily_context_from_client,
    gateway_config_from_settings,
    parse_fills,
    parse_open_trades,
    parse_portfolio_items,
    parse_summary_rows,
)


def make_settings(*, mode="paper", raw=None, base_currency="SGD") -> Settings:
    return Settings(
        mode=mode, config_mode=mode, base_currency=base_currency, account_verify={},
        strategy_style="blend", asset_classes=("stock",), new_ideas_count=5,
        risk=RiskLimits(5000, 15, 35, 3, 5, 8, 3, 22), management={}, universe={}, schedule={},
        recommend_skill_path="x", grader_skill_path="y", journal_db_path="j.db",
        raw=raw or {},
    )


# --- ib_async-shaped fakes -------------------------------------------------

class Row:
    def __init__(self, tag, value, currency=""):
        self.tag, self.value, self.currency = tag, value, currency


class Contract:
    def __init__(self, symbol, secType="STK", currency="USD"):
        self.symbol, self.secType, self.currency = symbol, secType, currency


class PortItem:
    def __init__(self, symbol, position, marketPrice, marketValue, averageCost, unrealizedPNL,
                 secType="STK"):
        self.contract = Contract(symbol, secType)
        self.position, self.marketPrice, self.marketValue = position, marketPrice, marketValue
        self.averageCost, self.unrealizedPNL = averageCost, unrealizedPNL


class FakeIB:
    def __init__(self, summary=None, values=None, portfolio=None, fills=None, open_trades=None):
        self._summary = summary or []
        self._values = values or []
        self._portfolio = portfolio or []
        self._fills = fills or []
        self._open = open_trades or []
        self.placed = []
        self.qualified = []

    def isConnected(self):
        return True

    def accountSummary(self):
        return self._summary

    def accountValues(self):
        return self._values

    def portfolio(self):
        return self._portfolio

    def fills(self):
        return self._fills

    def openTrades(self):
        return self._open

    def managedAccounts(self):
        return ["DU123456"]

    def qualifyContracts(self, c):
        self.qualified.append(c)
        return [c]

    def placeOrder(self, contract, order):
        self.placed.append((contract, order))
        return type("T", (), {"order": order})()


# --- parsing ---------------------------------------------------------------

def test_parse_summary_maps_ib_tags():
    s = parse_summary_rows([
        Row("NetLiquidation", "250015.07", "SGD"),
        Row("TotalCashValue", "250000", "SGD"),
        Row("BuyingPower", "1666767.13", "SGD"),
        Row("ExcessLiquidity", "250015.07", "SGD"),
        Row("Unmapped", "999", "SGD"),
    ], base_currency="SGD")
    assert s.net_liquidation == pytest.approx(250015.07)
    assert s.total_cash_value == pytest.approx(250000)
    assert s.currency == "SGD"
    assert s.is_funded is True


def test_parse_summary_prefers_base_currency_rows():
    s = parse_summary_rows([
        Row("NetLiquidation", "100", "SGD"),
        Row("NetLiquidation", "77", "USD"),   # duplicate in another currency, must not win
    ], base_currency="SGD")
    assert s.net_liquidation == pytest.approx(100)


def test_parse_portfolio_items_to_positions():
    ps = parse_portfolio_items([
        PortItem("NVDA", 100, 500.0, 50000.0, 400.0, 10000.0),
        PortItem("SPY", 10, 600.0, 6000.0, 590.0, 100.0, secType="OPT"),
    ])
    assert [p.symbol for p in ps] == ["NVDA", "SPY"]
    assert ps[0].unrealized_pnl_pct == pytest.approx(25.0)
    assert ps[1].asset_class == "option"


def test_parse_fills_and_open_trades():
    fill = type("F", (), {
        "contract": Contract("AAPL"),
        "execution": type("E", (), {"execId": "x1", "side": "BOT", "shares": 5, "price": 190.0,
                                    "time": "2026-07-28"})(),
        "commissionReport": type("C", (), {"commission": 1.0})(),
    })()
    trades = parse_fills([fill])
    assert trades[0].symbol == "AAPL" and trades[0].size == 5

    ot = type("T", (), {
        "contract": Contract("MSFT"),
        "order": type("O", (), {"orderId": 7, "action": "BUY", "orderType": "LMT",
                                "totalQuantity": 3, "lmtPrice": 400.0})(),
        "orderStatus": type("S", (), {"status": "PreSubmitted", "filled": 0})(),
    })()
    orders = parse_open_trades([ot])
    assert orders[0].symbol == "MSFT" and orders[0].status == "PreSubmitted"


# --- client ----------------------------------------------------------------

def test_client_reads_summary_positions_balances():
    ib = FakeIB(
        summary=[Row("NetLiquidation", "250015.07", "SGD"), Row("TotalCashValue", "250000", "SGD")],
        values=[Row("CashBalance", "250000", "SGD"), Row("StockMarketValue", "0", "SGD"),
                Row("CashBalance", "1", "BASE")],  # BASE must be skipped
        portfolio=[PortItem("NVDA", 100, 500.0, 50000.0, 400.0, 10000.0)],
    )
    c = IBGatewayBrokerClient(ib, GatewayConfig(), base_currency="SGD")
    assert c.account_summary().net_liquidation == pytest.approx(250015.07)
    assert [p.symbol for p in c.positions()] == ["NVDA"]
    bals = c.balances()
    assert [b.currency for b in bals] == ["SGD"]
    assert bals[0].cash_balance == pytest.approx(250000)


def test_account_meta_uses_managed_account():
    c = IBGatewayBrokerClient(FakeIB(), GatewayConfig(), base_currency="SGD")
    assert c.account_meta().account_key == "DU123456"


# --- safety: mode/port interlock ------------------------------------------

def test_paper_config_refuses_live_port():
    with pytest.raises(GatewayError, match="LIVE trading port"):
        check_mode_port(make_settings(mode="paper"), GatewayConfig(port=4001))


def test_live_config_refuses_paper_port():
    with pytest.raises(GatewayError, match="paper port"):
        check_mode_port(make_settings(mode="live"), GatewayConfig(port=4002))


def test_matching_pairs_are_allowed():
    check_mode_port(make_settings(mode="paper"), GatewayConfig(port=4002))
    check_mode_port(make_settings(mode="live"), GatewayConfig(port=4001))


def test_config_block_is_read():
    cfg = gateway_config_from_settings(make_settings(raw={"gateway": {
        "host": "10.0.0.5", "port": 7497, "client_id": 42, "readonly": False}}))
    assert (cfg.host, cfg.port, cfg.client_id, cfg.readonly) == ("10.0.0.5", 7497, 42, False)


# --- safety: staging never transmits --------------------------------------

def test_stager_refuses_readonly_connection():
    with pytest.raises(GatewayError, match="read-only"):
        GatewayOrderStager(FakeIB(), GatewayConfig(readonly=True))


def test_staged_order_is_never_transmitted(monkeypatch):
    """The whole safety model in one assertion: transmit must be False."""
    class LimitOrder:
        def __init__(self, action, qty, price):
            self.action, self.totalQuantity, self.lmtPrice = action, qty, price
            self.orderId = 1
            self.transmit = True          # start True to prove we actively set it False

    class Stock:
        def __init__(self, symbol, exchange, currency):
            self.symbol, self.exchange, self.currency = symbol, exchange, currency

    import sys
    fake_mod = type(sys)("ib_async")
    fake_mod.LimitOrder, fake_mod.Stock = LimitOrder, Stock
    monkeypatch.setitem(sys.modules, "ib_async", fake_mod)

    ib = FakeIB()
    stager = GatewayOrderStager(ib, GatewayConfig(readonly=False))
    proposal = type("P", (), {"symbol": "NVDA", "side": "BUY", "quantity": 10,
                              "limit_price": 120.0, "currency": "USD"})()
    result = stager.stage(proposal)

    contract, order = ib.placed[0]
    assert order.transmit is False          # ← never transmitted
    assert result["transmitted"] is False
    assert contract.symbol == "NVDA" and order.totalQuantity == 10


def test_stager_rejects_malformed_proposal(monkeypatch):
    import sys
    fake_mod = type(sys)("ib_async")
    fake_mod.LimitOrder = fake_mod.Stock = object
    monkeypatch.setitem(sys.modules, "ib_async", fake_mod)
    stager = GatewayOrderStager(FakeIB(), GatewayConfig(readonly=False))
    with pytest.raises(GatewayError, match="malformed"):
        stager.stage(type("P", (), {"symbol": "", "quantity": 0})())


# --- context wiring --------------------------------------------------------

def test_daily_context_from_client_feeds_the_review():
    ib = FakeIB(
        summary=[Row("NetLiquidation", "250000", "SGD"), Row("TotalCashValue", "200000", "SGD")],
        portfolio=[PortItem("NVDA", 100, 500.0, 50000.0, 400.0, 10000.0)],
    )
    client = IBGatewayBrokerClient(ib, GatewayConfig(), base_currency="SGD")
    ctx = daily_context_from_client(client, make_settings(), identity_verified=True)

    positions = ctx.fetch_positions()
    assert positions[0]["symbol"] == "NVDA" and positions[0]["market_value"] == 50000.0
    balances = ctx.fetch_balances()
    assert balances["net_liquidation"] == pytest.approx(250000)
    assert balances["cash"] == pytest.approx(200000)
    assert ctx.identity_verified is True


def test_runtime_selects_gateway_transport():
    from agent.runtime import transport_name
    assert transport_name(make_settings()) == "mcp"
    assert transport_name(make_settings(raw={"gateway": {"transport": "gateway"}})) == "gateway"
