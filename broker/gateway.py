"""Self-hosted IBKR transport — talk to IB Gateway / TWS directly instead of the MCP connector.

Why this exists
---------------
The MCP connector works beautifully in an interactive Claude session but does **not respond in
a headless scheduled session** (see docs/RUNNING.md, "Known limitation"). That makes unattended
runs impossible through it. This module is the alternative: a direct socket connection to a
locally-running **IB Gateway** (or TWS) via ``ib_async``, so a cron job / systemd timer on your
own VM can read the account and stage orders with no Claude session involved.

It implements the SAME :class:`broker.client.BrokerClient` protocol as ``MCPBrokerClient``, so
everything downstream — the review, risk layer, dashboard, journal — is unchanged. Which
transport you get is a config choice, not a code change.

The safety model is preserved, and in fact enforced harder
---------------------------------------------------------
Orders are placed with **``transmit=False``**. That is IBKR's native "stage for review": the
order appears in TWS/Gateway pre-filled and waits for *you* to press Transmit. It is not live,
it is not working, and it will never fill on its own. There is no code path in this module that
sets ``transmit=True`` — the flag is hard-coded, not a parameter — so the "never executes"
guarantee survives even when nobody is watching.

Additional guards:
* the paper/live interlock still applies (``Settings.is_live``); a live connection additionally
  requires the port to be a live-trading port, and we refuse an obviously mismatched pairing;
* the client connects **read-only by default** (``readonly=True``), which makes order placement
  impossible at the API level. Staging must be explicitly enabled.

``ib_async`` is an OPTIONAL dependency (``pip install -e ".[gateway]"``). The import is deferred
so the rest of the package — and the whole test suite — works without it installed. Every
method takes an injectable ``ib`` object, so this module is fully testable with a fake.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from broker.client import (
    AccountMeta,
    AccountSummary,
    CurrencyBalance,
    Order,
    Position,
    Trade,
    _f,
)

# IB Gateway / TWS default ports. Paper and live are different ports by design — that is our
# second interlock: a paper config cannot reach the live port by accident.
PAPER_PORTS = (7497, 4002)   # TWS paper, Gateway paper
LIVE_PORTS = (7496, 4001)    # TWS live,  Gateway live


class GatewayError(RuntimeError):
    """Raised for connection problems and for unsafe mode/port pairings."""


@dataclass(frozen=True)
class GatewayConfig:
    """Connection settings for a self-hosted IB Gateway / TWS."""

    host: str = "127.0.0.1"
    port: int = 4002              # Gateway paper by default
    client_id: int = 17
    account: str = ""             # optional; needed only on multi-account logins
    readonly: bool = True         # API-level block on order placement
    timeout: float = 20.0

    @property
    def is_live_port(self) -> bool:
        return self.port in LIVE_PORTS


def gateway_config_from_settings(settings: Any) -> GatewayConfig:
    """Read the ``gateway:`` block out of config.yaml (all keys optional)."""
    raw = dict((getattr(settings, "raw", {}) or {}).get("gateway", {}) or {})
    return GatewayConfig(
        host=str(raw.get("host", "127.0.0.1")),
        port=int(raw.get("port", 4002)),
        client_id=int(raw.get("client_id", 17)),
        account=str(raw.get("account", "") or ""),
        readonly=bool(raw.get("readonly", True)),
        timeout=float(raw.get("timeout", 20.0)),
    )


def check_mode_port(settings: Any, cfg: GatewayConfig) -> None:
    """Refuse mode/port pairings that would surprise you.

    Live config + paper port is merely useless; **paper config + live port is dangerous**, so
    that one is a hard error. This runs before any connection is made.
    """
    is_live = bool(getattr(settings, "is_live", False))
    if not is_live and cfg.is_live_port:
        raise GatewayError(
            f"refusing to connect: mode is PAPER but port {cfg.port} is a LIVE trading port. "
            f"Use a paper port {PAPER_PORTS} or set account.mode: live + IBKR_ALLOW_LIVE=1."
        )
    if is_live and not cfg.is_live_port:
        raise GatewayError(
            f"mode is LIVE but port {cfg.port} is a paper port. Point at a live port "
            f"{LIVE_PORTS} or switch back to paper (`python -m broker.mode paper`)."
        )


def connect(cfg: GatewayConfig, *, ib: Any | None = None) -> Any:
    """Connect to IB Gateway/TWS and return the ``IB`` handle.

    Pass ``ib`` to inject a fake (tests) or an already-connected handle.
    """
    if ib is None:
        try:
            from ib_async import IB  # deferred: optional dependency
        except ImportError as exc:  # pragma: no cover - exercised only without the extra
            raise GatewayError(
                "ib_async is not installed. Install the gateway extra:\n"
                '    pip install -e ".[gateway]"'
            ) from exc
        ib = IB()
    if not getattr(ib, "isConnected", lambda: False)():
        ib.connect(cfg.host, cfg.port, clientId=cfg.client_id,
                   readonly=cfg.readonly, timeout=cfg.timeout)
    return ib


# --------------------------------------------------------------------------- parsing helpers

# IB account-summary tag -> our AccountSummary field.
_TAG_MAP = {
    "NetLiquidation": "net_liquidation",
    "EquityWithLoanValue": "equity_with_loan_value",
    "BuyingPower": "buying_power",
    "GrossPositionValue": "gross_position_value",
    "TotalCashValue": "total_cash_value",
    "AvailableFunds": "available_funds",
    "FullInitMarginReq": "initial_margin",
    "InitMarginReq": "initial_margin",
    "FullMaintMarginReq": "maintenance_margin",
    "MaintMarginReq": "maintenance_margin",
    "ExcessLiquidity": "excess_liquidity",
    "Leverage": "leverage",
}


def _attr(obj: Any, *names: str, default: Any = None) -> Any:
    """Read the first present attribute (ib_async objects) or dict key (fakes/JSON)."""
    for n in names:
        if isinstance(obj, dict):
            if obj.get(n) is not None:
                return obj[n]
        elif getattr(obj, n, None) is not None:
            return getattr(obj, n)
    return default


def parse_summary_rows(rows: list[Any], *, base_currency: str = "") -> AccountSummary:
    """Turn ``ib.accountSummary()`` AccountValue rows into our AccountSummary."""
    fields: dict[str, float] = {}
    currency = base_currency
    for r in rows:
        tag = str(_attr(r, "tag", default=""))
        field = _TAG_MAP.get(tag)
        if not field:
            continue
        # Prefer rows denominated in the base currency; IB also emits per-currency duplicates.
        row_ccy = str(_attr(r, "currency", default="") or "")
        if row_ccy and base_currency and row_ccy != base_currency and field in fields:
            continue
        fields[field] = _f(_attr(r, "value", default=0))
        if row_ccy and not currency:
            currency = row_ccy
    return AccountSummary(
        currency=currency,
        net_liquidation=fields.get("net_liquidation", 0.0),
        equity_with_loan_value=fields.get("equity_with_loan_value", 0.0),
        buying_power=fields.get("buying_power", 0.0),
        gross_position_value=fields.get("gross_position_value", 0.0),
        total_cash_value=fields.get("total_cash_value", 0.0),
        available_funds=fields.get("available_funds", 0.0),
        initial_margin=fields.get("initial_margin", 0.0),
        maintenance_margin=fields.get("maintenance_margin", 0.0),
        excess_liquidity=fields.get("excess_liquidity", 0.0),
        dividends=0.0,  # not exposed as a summary tag
        leverage=fields.get("leverage", 0.0),
        raw={"rows": len(rows)},
    )


def parse_portfolio_items(items: list[Any]) -> list[Position]:
    """Turn ``ib.portfolio()`` PortfolioItems into Positions (has market value + P&L)."""
    out: list[Position] = []
    for it in items:
        contract = _attr(it, "contract", default=None)
        symbol = str(_attr(contract, "symbol", "localSymbol", default="") if contract else
                     _attr(it, "symbol", default=""))
        if not symbol:
            continue
        sec_type = str(_attr(contract, "secType", default="STK") if contract else "STK")
        out.append(Position(
            symbol=symbol,
            quantity=_f(_attr(it, "position", "quantity", default=0)),
            price=_f(_attr(it, "marketPrice", "market_price", default=0)),
            market_value=_f(_attr(it, "marketValue", "market_value", default=0)),
            avg_cost=_f(_attr(it, "averageCost", "avgCost", default=0)),
            unrealized_pnl=_f(_attr(it, "unrealizedPNL", "unrealized_pnl", default=0)),
            asset_class=_sec_type_to_asset_class(sec_type),
            currency=str(_attr(contract, "currency", default="") if contract else ""),
            raw={},
        ))
    return out


def _sec_type_to_asset_class(sec_type: str) -> str:
    st = (sec_type or "").upper()
    if st == "STK":
        return "stock"
    if st == "OPT":
        return "option"
    if st == "FUT":
        return "future"
    if st in ("FUND", "ETF"):
        return "etf"
    return st.lower() or "stock"


def parse_fills(fills: list[Any]) -> list[Trade]:
    """Turn ``ib.fills()`` into Trades."""
    out: list[Trade] = []
    for f in fills:
        contract = _attr(f, "contract", default=None)
        execution = _attr(f, "execution", default=None)
        report = _attr(f, "commissionReport", default=None)
        out.append(Trade(
            trade_id=str(_attr(execution, "execId", default="") if execution else ""),
            symbol=str(_attr(contract, "symbol", default="") if contract else ""),
            side=str(_attr(execution, "side", default="") if execution else "").upper(),
            size=_f(_attr(execution, "shares", "size", default=0) if execution else 0),
            price=_f(_attr(execution, "price", default=0) if execution else 0),
            commission=_f(_attr(report, "commission", default=0) if report else 0),
            trade_time=str(_attr(execution, "time", default="") if execution else ""),
            currency=str(_attr(contract, "currency", default="") if contract else ""),
            raw={},
        ))
    return out


def parse_open_trades(trades: list[Any]) -> list[Order]:
    """Turn ``ib.openTrades()`` into Orders (includes our staged, untransmitted ones)."""
    out: list[Order] = []
    for t in trades:
        contract = _attr(t, "contract", default=None)
        order = _attr(t, "order", default=None)
        status = _attr(t, "orderStatus", default=None)
        out.append(Order(
            order_id=str(_attr(order, "orderId", default="") if order else ""),
            symbol=str(_attr(contract, "symbol", default="") if contract else ""),
            side=str(_attr(order, "action", default="") if order else "").upper(),
            order_type=str(_attr(order, "orderType", default="") if order else ""),
            status=str(_attr(status, "status", default="") if status else ""),
            quantity=_f(_attr(order, "totalQuantity", default=0) if order else 0),
            price=_f(_attr(order, "lmtPrice", "auxPrice", default=0) if order else 0),
            filled=_f(_attr(status, "filled", default=0) if status else 0),
            raw={},
        ))
    return out


# --------------------------------------------------------------------------- client


class IBGatewayBrokerClient:
    """:class:`broker.client.BrokerClient` backed by a local IB Gateway / TWS.

    Drop-in replacement for ``MCPBrokerClient``: same methods, same dataclasses, so the review,
    risk layer, dashboard and journal are untouched.
    """

    def __init__(self, ib: Any, cfg: GatewayConfig | None = None, base_currency: str = ""):
        self._ib = ib
        self._cfg = cfg or GatewayConfig()
        self._base_currency = base_currency

    # -- reads ------------------------------------------------------------

    def account_summary(self) -> AccountSummary:
        return parse_summary_rows(list(self._ib.accountSummary() or []),
                                  base_currency=self._base_currency)

    def account_meta(self) -> AccountMeta:
        """Identity signals available over the API.

        Unlike the MCP connector (which masks it), the Gateway *does* expose the account id.
        We deliberately keep it out of ``AccountMeta.account_key`` display paths — the review
        never prints it — but it is available for the verify check.
        """
        accounts = list(getattr(self._ib, "managedAccounts", lambda: [])() or [])
        return AccountMeta(
            base_currency=self._base_currency,
            inception_date="",
            last_update="",
            account_key=str(self._cfg.account or (accounts[0] if accounts else "account")),
            raw={"managed_accounts": len(accounts)},
        )

    def balances(self) -> list[CurrencyBalance]:
        """Per-currency cash from ``ib.accountValues()``."""
        by_ccy: dict[str, dict[str, float]] = {}
        for v in self._ib.accountValues() or []:
            ccy = str(_attr(v, "currency", default="") or "")
            if not ccy or ccy == "BASE":
                continue
            tag = str(_attr(v, "tag", default=""))
            slot = by_ccy.setdefault(ccy, {})
            if tag == "CashBalance":
                slot["cash"] = _f(_attr(v, "value", default=0))
            elif tag == "TotalCashBalance":
                slot.setdefault("settled", _f(_attr(v, "value", default=0)))
            elif tag == "StockMarketValue":
                slot["stock"] = _f(_attr(v, "value", default=0))
            elif tag == "UnrealizedPnL":
                slot["upnl"] = _f(_attr(v, "value", default=0))
            elif tag == "RealizedPnL":
                slot["rpnl"] = _f(_attr(v, "value", default=0))
            elif tag == "NetLiquidationByCurrency":
                slot["netliq"] = _f(_attr(v, "value", default=0))
        return [
            CurrencyBalance(
                currency=ccy,
                cash_balance=s.get("cash", 0.0),
                settled_cash=s.get("settled", s.get("cash", 0.0)),
                net_liquidation_value=s.get("netliq", 0.0),
                stock_market_value=s.get("stock", 0.0),
                unrealized_pnl=s.get("upnl", 0.0),
                realized_pnl=s.get("rpnl", 0.0),
                exchange_rate=1.0,
            )
            for ccy, s in sorted(by_ccy.items())
        ]

    def positions(self) -> list[Position]:
        return parse_portfolio_items(list(self._ib.portfolio() or []))

    def trades(self, period: str = "DAYS_90") -> list[Trade]:
        """Fills known to this session. IB's API exposes recent activity, not full history —
        for older periods use Flex reports; the period arg is accepted for protocol parity."""
        return parse_fills(list(self._ib.fills() or []))

    def orders(self) -> list[Order]:
        return parse_open_trades(list(self._ib.openTrades() or []))


# --------------------------------------------------------------------------- staging


class GatewayOrderStager:
    """Stage orders into TWS/Gateway for your manual approval — never transmits.

    ``transmit=False`` is hard-coded. The order shows up in TWS pre-filled and inert; you press
    Transmit (or cancel it). Nothing here can send an order to the exchange, which is the same
    guarantee the MCP path gets from ``create_order_instruction``.
    """

    def __init__(self, ib: Any, cfg: GatewayConfig | None = None):
        self._ib = ib
        self._cfg = cfg or GatewayConfig()
        if self._cfg.readonly:
            raise GatewayError(
                "cannot stage orders on a read-only connection. Set gateway.readonly: false "
                "in config.yaml to allow staging (orders are still never transmitted)."
            )

    def stage(self, proposal: Any) -> dict[str, Any]:
        """Place ``proposal`` as an untransmitted limit order. Returns a small summary."""
        from ib_async import LimitOrder, Stock  # deferred: optional dependency

        symbol = str(getattr(proposal, "symbol", ""))
        side = str(getattr(proposal, "side", "BUY")).upper()
        qty = abs(float(getattr(proposal, "quantity", 0)))
        limit = float(getattr(proposal, "limit_price", 0) or 0)
        if not symbol or qty <= 0:
            raise GatewayError(f"refusing to stage a malformed proposal: {proposal!r}")

        contract = Stock(symbol, "SMART", getattr(proposal, "currency", "") or "USD")
        self._ib.qualifyContracts(contract)

        order = LimitOrder(side, qty, limit)
        order.transmit = False          # ← the guarantee. Never set to True anywhere.
        order.outsideRth = False
        trade = self._ib.placeOrder(contract, order)
        return {
            "symbol": symbol, "side": side, "quantity": qty, "limit_price": limit,
            "transmitted": False,
            "order_id": str(getattr(getattr(trade, "order", None), "orderId", "")),
        }


# --------------------------------------------------------------------------- wiring


def build_gateway_client(settings: Any, *, ib: Any | None = None) -> IBGatewayBrokerClient:
    """Connect (unless ``ib`` is injected) and return a client bound to the Gateway."""
    cfg = gateway_config_from_settings(settings)
    check_mode_port(settings, cfg)
    handle = connect(cfg, ib=ib)
    return IBGatewayBrokerClient(handle, cfg, base_currency=getattr(settings, "base_currency", ""))


def daily_context_from_client(
    client: Any,
    settings: Any,
    *,
    identity_verified: bool | None = None,
    grade_symbol: Callable[[str], Any] | None = None,
    recommend_new: Callable[[int], list] | None = None,
    load_signals: Callable[[], list] | None = None,
    assess_market: Callable[[], str] | None = None,
    stage_order: Callable[[Any], None] | None = None,
):
    """Build a :class:`workflows.daily_review.DailyContext` from any BrokerClient.

    This is what makes an unattended run possible: account data comes from the Gateway. The
    *analysis* callables (grading, screening) still need a model — supply them from an Agent SDK
    loop, or leave them out and the review runs in informational mode.
    """
    from workflows.daily_review import DailyContext

    def _positions() -> list[dict[str, Any]]:
        return [
            {"symbol": p.symbol, "position": p.quantity, "market_value": p.market_value,
             "avg_cost": p.avg_cost, "price": p.price, "unrealized_pnl": p.unrealized_pnl,
             "asset_class": p.asset_class, "currency": p.currency}
            for p in client.positions()
        ]

    def _balances() -> dict[str, Any]:
        s = client.account_summary()
        return {"net_liquidation": s.net_liquidation, "cash": s.total_cash_value}

    kwargs: dict[str, Any] = {
        "fetch_positions": _positions,
        "fetch_balances": _balances,
        "identity_verified": identity_verified,
    }
    if grade_symbol is not None:
        kwargs["grade_symbol"] = grade_symbol
    if recommend_new is not None:
        kwargs["recommend_new"] = recommend_new
    if load_signals is not None:
        kwargs["load_signals"] = load_signals
    if assess_market is not None:
        kwargs["assess_market"] = assess_market
    if stage_order is not None:
        kwargs["stage_order"] = stage_order
    return DailyContext(**kwargs)


# --------------------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    from agent.settings import load_settings

    parser = argparse.ArgumentParser(
        description="Self-hosted IB Gateway transport: check connectivity and read the account.")
    parser.add_argument("--check", action="store_true",
                        help="Connect and print a connectivity + identity report, then exit.")
    parser.add_argument("--account", action="store_true",
                        help="Print the account summary, balances and positions.")
    parser.add_argument("--config", default=None)
    args = parser.parse_args(argv)

    settings = load_settings(args.config)
    cfg = gateway_config_from_settings(settings)

    from agent.runtime import mode_banner
    print(mode_banner(settings))
    print(f"Gateway target: {cfg.host}:{cfg.port} (clientId={cfg.client_id}, "
          f"readonly={cfg.readonly}) — {'LIVE' if cfg.is_live_port else 'paper'} port")

    try:
        check_mode_port(settings, cfg)
        client = build_gateway_client(settings)
    except GatewayError as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - connection/transport errors surface cleanly
        print(f"\nCould not connect to IB Gateway at {cfg.host}:{cfg.port}: {exc}",
              file=sys.stderr)
        print("Is IB Gateway running and is the API enabled "
              "(Configure > Settings > API > Enable ActiveX and Socket Clients)?",
              file=sys.stderr)
        return 2

    summary = client.account_summary()
    print(f"\nConnected. Net liquidation: {summary.net_liquidation:,.2f} {summary.currency}")
    print(f"Funded: {summary.is_funded}")

    if args.account or not args.check:
        print(f"  Cash            : {summary.total_cash_value:,.2f}")
        print(f"  Available funds : {summary.available_funds:,.2f}")
        print(f"  Buying power    : {summary.buying_power:,.2f}")
        positions = client.positions()
        print(f"\nPositions ({len(positions)}):")
        for p in positions or []:
            print(f"  {p.symbol:<8} {p.quantity:>10,.2f} @ {p.price:,.2f}  "
                  f"mkt {p.market_value:,.2f}  ({p.unrealized_pnl_pct:+.1f}%)")
        orders = client.orders()
        print(f"\nWorking/staged orders ({len(orders)}):")
        for o in orders or []:
            print(f"  {o.symbol:<8} {o.side:<4} {o.quantity:>8,.2f} {o.order_type} "
                  f"@ {o.price:,.2f}  [{o.status}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
