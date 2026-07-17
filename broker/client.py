"""IBKR account data access.

Typed dataclasses + parsers for the IBKR MCP connector's account tools, plus two client
implementations:

* ``MCPBrokerClient`` — calls the real connector through an injected *tool caller* (a
  ``(tool_name, args) -> dict`` callable). Keeping the transport injected makes this usable
  from the hosted Agent SDK loop, a standalone SDK MCP session, or a test double, without the
  data-access code caring which. The concrete tool caller is bound at ``TODO(connector)``.
* ``StaticBrokerClient`` — returns canned responses; seeded in tests with the exact shapes
  captured from the live connector so parsing is tested against reality.

Field parsing is grounded in the connector's real responses (see tests/fixtures) and stays
defensive on the collection tools (positions/trades/orders) whose exact keys can vary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, runtime_checkable

# A transport-agnostic MCP tool caller: given a fully-qualified tool name and its args,
# return the parsed JSON object the tool produced.
ToolCaller = Callable[[str, dict[str, Any]], dict[str, Any]]

# Default connector tool prefix. The hosted session prefixes IBKR tools with the connector's
# id; callers can override if theirs differs.
DEFAULT_TOOL_PREFIX = "mcp__Interactive_Brokers_IBKR__"


# --------------------------------------------------------------------------- models


@dataclass(frozen=True)
class AccountSummary:
    currency: str
    net_liquidation: float
    equity_with_loan_value: float
    buying_power: float
    gross_position_value: float
    total_cash_value: float
    available_funds: float
    initial_margin: float
    maintenance_margin: float
    excess_liquidity: float
    dividends: float
    leverage: float
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def is_funded(self) -> bool:
        return self.net_liquidation > 0 or self.total_cash_value > 0


@dataclass(frozen=True)
class AccountMeta:
    """Identity signals the connector *does* expose.

    NOTE: the IBKR MCP connector masks the account number and the owner's name by design —
    the performance tool returns the account under a generic ``"account"`` key, never a
    ``U#######`` id, and no tool returns the holder name. Identity is therefore established
    from these non-sensitive signals plus the fingerprint, not from a number/name.
    """

    base_currency: str
    inception_date: str      # account "start" date (yyyymmdd) — a strong recognizer
    last_update: str
    account_key: str         # the connector's opaque key (usually the literal "account")
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class CurrencyBalance:
    currency: str
    cash_balance: float
    settled_cash: float
    net_liquidation_value: float
    stock_market_value: float
    unrealized_pnl: float
    realized_pnl: float
    exchange_rate: float


@dataclass(frozen=True)
class Position:
    symbol: str
    quantity: float
    price: float
    market_value: float
    avg_cost: float
    unrealized_pnl: float
    asset_class: str
    currency: str = ""
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def unrealized_pnl_pct(self) -> float:
        cost_basis = abs(self.avg_cost * self.quantity)
        return 0.0 if cost_basis == 0 else (self.market_value - cost_basis) / cost_basis * 100.0


@dataclass(frozen=True)
class Trade:
    trade_id: str
    symbol: str
    side: str
    size: float
    price: float
    commission: float
    trade_time: str
    currency: str = ""
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class Order:
    order_id: str
    symbol: str
    side: str
    order_type: str
    status: str
    quantity: float
    price: float
    filled: float
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


# --------------------------------------------------------------------------- parsers


def _f(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _first(d: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


def parse_account_summary(data: dict[str, Any]) -> AccountSummary:
    return AccountSummary(
        currency=str(_first(data, "currency", "base_currency", default="")),
        net_liquidation=_f(_first(data, "net_liquidation", "net_liquidation_value")),
        equity_with_loan_value=_f(data.get("equity_with_loan_value")),
        buying_power=_f(data.get("buying_power")),
        gross_position_value=_f(data.get("gross_position_value")),
        total_cash_value=_f(_first(data, "total_cash_value", "total_cash")),
        available_funds=_f(data.get("available_funds")),
        initial_margin=_f(data.get("initial_margin")),
        maintenance_margin=_f(data.get("maintenance_margin")),
        excess_liquidity=_f(data.get("excess_liquidity")),
        dividends=_f(data.get("dividends")),
        leverage=_f(data.get("leverage")),
        raw=data,
    )


def parse_account_meta(data: dict[str, Any]) -> AccountMeta:
    """Parse identity signals from a ``get_pa_performance_all_periods`` response."""
    accounts = data.get("accounts", {}) or {}
    # The connector nests the account under an opaque key (typically the literal "account").
    account_key = next(iter(accounts), "")
    acct = accounts.get(account_key, {}) if isinstance(accounts, dict) else {}
    return AccountMeta(
        base_currency=str(_first(acct, "base_currency", default=data.get("currency", ""))),
        inception_date=str(_first(acct, "start", "inception", default="")),
        last_update=str(_first(acct, "last_successful_update", "end", default="")),
        account_key=str(account_key),
        raw=data,
    )


def parse_balances(data: dict[str, Any]) -> list[CurrencyBalance]:
    out: list[CurrencyBalance] = []
    for b in data.get("balances", []) or []:
        out.append(
            CurrencyBalance(
                currency=str(b.get("currency", "")),
                cash_balance=_f(b.get("cash_balance")),
                settled_cash=_f(b.get("settled_cash")),
                net_liquidation_value=_f(b.get("net_liquidation_value")),
                stock_market_value=_f(b.get("stock_market_value")),
                unrealized_pnl=_f(b.get("unrealized_pnl")),
                realized_pnl=_f(b.get("realized_pnl")),
                exchange_rate=_f(b.get("exchange_rate"), 1.0),
            )
        )
    return out


def parse_positions(data: dict[str, Any]) -> list[Position]:
    out: list[Position] = []
    for p in data.get("positions", []) or []:
        qty = _f(_first(p, "position", "quantity", "qty", "size"))
        avg = _f(_first(p, "avg_cost", "avgCost", "average_cost", "cost_basis"))
        mkt = _f(_first(p, "market_value", "mktValue", "value"))
        out.append(
            Position(
                symbol=str(_first(p, "symbol", "ticker", default="")),
                quantity=qty,
                price=_f(_first(p, "price", "market_price", "mkt_price")),
                market_value=mkt,
                avg_cost=avg,
                unrealized_pnl=_f(_first(p, "unrealized_pnl", "unrealizedPnl", "pnl")),
                asset_class=str(_first(p, "asset_class", "sec_type", "secType", default="stock")).lower(),
                currency=str(_first(p, "currency", default="")),
                raw=p,
            )
        )
    return out


def parse_trades(data: dict[str, Any]) -> list[Trade]:
    out: list[Trade] = []
    for t in data.get("trades", []) or []:
        out.append(
            Trade(
                trade_id=str(_first(t, "trade_id", "tradeId", "execution_id", "id", default="")),
                symbol=str(_first(t, "symbol", "ticker", default="")),
                side=str(_first(t, "side", "action", default="")).upper(),
                size=_f(_first(t, "size", "quantity", "shares")),
                price=_f(t.get("price")),
                commission=_f(_first(t, "commission", "commissions", "fee")),
                trade_time=str(_first(t, "trade_time", "time", "timestamp", "date", default="")),
                currency=str(_first(t, "currency", default="")),
                raw=t,
            )
        )
    return out


def parse_orders(data: dict[str, Any]) -> list[Order]:
    out: list[Order] = []
    for o in data.get("orders", []) or []:
        out.append(
            Order(
                order_id=str(_first(o, "order_id", "orderId", "id", default="")),
                symbol=str(_first(o, "symbol", "ticker", default="")),
                side=str(_first(o, "side", "action", default="")).upper(),
                order_type=str(_first(o, "order_type", "orderType", "type", default="")),
                status=str(_first(o, "status", "order_status", default="")),
                quantity=_f(_first(o, "quantity", "size", "total_quantity")),
                price=_f(_first(o, "price", "limit_price", "aux_price")),
                filled=_f(_first(o, "filled", "filled_quantity", "cum_qty")),
                raw=o,
            )
        )
    return out


# --------------------------------------------------------------------------- clients


@runtime_checkable
class BrokerClient(Protocol):
    """Read-only account access. No method here mutates the account."""

    def account_summary(self) -> AccountSummary: ...
    def account_meta(self) -> AccountMeta: ...
    def balances(self) -> list[CurrencyBalance]: ...
    def positions(self) -> list[Position]: ...
    def trades(self, period: str = "DAYS_90") -> list[Trade]: ...
    def orders(self) -> list[Order]: ...


class MCPBrokerClient:
    """BrokerClient backed by the IBKR MCP connector via an injected tool caller.

    TODO(connector): bind ``tool_caller`` to the live transport. In the hosted Agent SDK loop
    that is the SDK's MCP invocation; standalone, construct an SDK MCP client and adapt its
    call method to the ``ToolCaller`` signature.
    """

    def __init__(self, tool_caller: ToolCaller, tool_prefix: str = DEFAULT_TOOL_PREFIX):
        self._call = tool_caller
        self._prefix = tool_prefix

    def _tool(self, name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._call(f"{self._prefix}{name}", args or {})

    def account_summary(self) -> AccountSummary:
        return parse_account_summary(self._tool("get_account_summary"))

    def account_meta(self) -> AccountMeta:
        return parse_account_meta(self._tool("get_pa_performance_all_periods"))

    def balances(self) -> list[CurrencyBalance]:
        return parse_balances(self._tool("get_account_balances"))

    def positions(self) -> list[Position]:
        return parse_positions(self._tool("get_account_positions"))

    def trades(self, period: str = "DAYS_90") -> list[Trade]:
        return parse_trades(self._tool("get_account_trades", {"period": period}))

    def orders(self) -> list[Order]:
        return parse_orders(self._tool("get_account_orders"))


class StaticBrokerClient:
    """BrokerClient that replays canned tool responses. For tests and offline dry runs."""

    def __init__(
        self,
        summary: dict[str, Any] | None = None,
        balances: dict[str, Any] | None = None,
        positions: dict[str, Any] | None = None,
        trades: dict[str, Any] | None = None,
        orders: dict[str, Any] | None = None,
        performance: dict[str, Any] | None = None,
    ):
        self._summary = summary or {}
        self._balances = balances or {"balances": []}
        self._positions = positions or {"positions": []}
        self._trades = trades or {"trades": []}
        self._orders = orders or {"orders": []}
        self._performance = performance or {"accounts": {}}

    def account_summary(self) -> AccountSummary:
        return parse_account_summary(self._summary)

    def account_meta(self) -> AccountMeta:
        return parse_account_meta(self._performance)

    def balances(self) -> list[CurrencyBalance]:
        return parse_balances(self._balances)

    def positions(self) -> list[Position]:
        return parse_positions(self._positions)

    def trades(self, period: str = "DAYS_90") -> list[Trade]:
        return parse_trades(self._trades)

    def orders(self) -> list[Order]:
        return parse_orders(self._orders)
