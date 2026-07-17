"""Portfolio analytics computed from IBKR account data.

The functions here are pure: they turn raw position/balance dicts (as returned by the IBKR
MCP tools ``get_account_positions`` / ``get_account_balances``) into the ``PortfolioState``
the risk layer consumes and into human-readable exposure summaries. Fetching the raw data
is the agent's job (``TODO(connector)`` at the call sites in the workflow); parsing and math
live here so they can be unit tested without a live connector.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from risk.guardrails import PortfolioState


@dataclass(frozen=True)
class Holding:
    symbol: str
    quantity: float
    market_value: float
    avg_cost: float
    unrealized_pnl_pct: float
    sector: str
    asset_class: str
    # For options: days to expiry, else None.
    dte: int | None = None


def parse_holdings(
    raw_positions: list[dict[str, Any]],
    sector_by_symbol: dict[str, str] | None = None,
) -> list[Holding]:
    """Normalize IBKR position dicts into Holdings. Tolerant of field-name variants."""
    sector_by_symbol = sector_by_symbol or {}
    holdings: list[Holding] = []
    for p in raw_positions:
        symbol = str(p.get("symbol") or p.get("ticker") or p.get("contract", {}).get("symbol", ""))
        if not symbol:
            continue
        qty = _f(p.get("position") or p.get("quantity") or p.get("qty"))
        mkt = _f(p.get("market_value") or p.get("mktValue") or p.get("value"))
        avg = _f(p.get("avg_cost") or p.get("avgCost") or p.get("average_cost"))
        # Unrealized P&L %: prefer an explicit field, else derive from cost basis.
        pnl_pct = p.get("unrealized_pnl_pct")
        if pnl_pct is None and avg and qty:
            cost_basis = abs(avg * qty)
            pnl_pct = _pct(mkt - cost_basis, cost_basis) if cost_basis else 0.0
        holdings.append(
            Holding(
                symbol=symbol,
                quantity=qty,
                market_value=mkt,
                avg_cost=avg,
                unrealized_pnl_pct=float(pnl_pct or 0.0),
                sector=sector_by_symbol.get(symbol, str(p.get("sector", "UNKNOWN"))),
                asset_class=str(p.get("asset_class") or p.get("sec_type") or "stock").lower(),
                dte=_int_or_none(p.get("dte") or p.get("days_to_expiry")),
            )
        )
    return holdings


def build_portfolio_state(
    holdings: list[Holding],
    balances: dict[str, Any],
) -> PortfolioState:
    """Assemble the PortfolioState the risk layer needs."""
    equity = _f(
        balances.get("net_liquidation")
        or balances.get("netLiquidation")
        or balances.get("equity")
    )
    cash = _f(balances.get("cash") or balances.get("total_cash") or balances.get("availableFunds"))

    position_values: dict[str, float] = {}
    sector_values: dict[str, float] = {}
    for h in holdings:
        position_values[h.symbol] = position_values.get(h.symbol, 0.0) + h.market_value
        sector_values[h.sector] = sector_values.get(h.sector, 0.0) + h.market_value

    # Fall back to summing positions if net-liq wasn't provided.
    if equity <= 0:
        equity = sum(position_values.values()) + cash

    return PortfolioState(
        equity=equity,
        cash=cash,
        position_values=position_values,
        sector_values=sector_values,
    )


def exposure_summary(state: PortfolioState) -> dict[str, Any]:
    """A compact exposure report for the weekly brief."""
    def weights(d: dict[str, float]) -> dict[str, float]:
        return {
            k: round(_pct(v, state.equity), 1)
            for k, v in sorted(d.items(), key=lambda kv: -kv[1])
        }

    invested = sum(state.position_values.values())
    return {
        "equity": round(state.equity, 2),
        "cash": round(state.cash, 2),
        "cash_pct": round(_pct(state.cash, state.equity), 1),
        "invested_pct": round(_pct(invested, state.equity), 1),
        "position_weights_pct": weights(state.position_values),
        "sector_weights_pct": weights(state.sector_values),
    }


def _f(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _int_or_none(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _pct(part: float, whole: float) -> float:
    return 0.0 if whole <= 0 else (part / whole) * 100.0
