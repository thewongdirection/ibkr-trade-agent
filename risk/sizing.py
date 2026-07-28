"""Position sizing — shared by the weekly and daily reviews.

Turns a buy idea (a CAN SLIM recommendation or a monitor-signal candidate) into a concrete
``OrderProposal`` sized to the per-order notional cap and the investable cash left after the
cash buffer. Kept here (not in a workflow) so both review cadences size identically.
"""

from __future__ import annotations

from agent.settings import Settings
from risk.guardrails import OrderProposal, PortfolioState


def size_entry(
    *,
    symbol: str,
    price: float,
    stop: float | None,
    sector: str,
    reason: str,
    portfolio: PortfolioState,
    settings: Settings,
    asset_class: str = "stock",
) -> OrderProposal:
    """Size a new BUY entry to fit every cap at once.

    The budget is the smallest of: the per-order notional cap, investable cash left after the
    cash buffer, the remaining room under the position-weight cap for this symbol, and the
    remaining room under the sector-weight cap. Sizing to a single cap (e.g. per-order) would
    otherwise produce an order that the risk layer then rejects on a different cap.
    """
    r = settings.risk
    equity = portfolio.equity
    investable_cash = max(0.0, portfolio.cash - equity * (r.cash_buffer_pct / 100.0))
    position_room = max(
        0.0, equity * (r.max_position_weight_pct / 100.0)
        - portfolio.position_values.get(symbol, 0.0))
    sector_room = max(
        0.0, equity * (r.max_sector_weight_pct / 100.0)
        - portfolio.sector_values.get(sector, 0.0))
    budget = min(r.max_order_notional, investable_cash, position_room, sector_room)
    mult = 100.0 if asset_class == "option" else 1.0
    unit_cost = price * mult
    qty = 0 if unit_cost <= 0 else int(budget // unit_cost)
    return OrderProposal(
        symbol=symbol,
        side="BUY",
        quantity=float(qty),
        limit_price=price,
        asset_class=asset_class,
        sector=sector,
        stop_price=stop,
        rationale=reason,
    )
