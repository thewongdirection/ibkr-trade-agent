"""Risk guardrails — the checks every proposed order must pass BEFORE it is staged.

These are deterministic, unit-tested, and independent of the model. If any check fails the
order is rejected with a reason and never reaches ``create_order_instruction``.

Two entry points:
  * ``check_order_instruction`` — called by the agent permission callback on the raw MCP
    tool payload (a defensive, last-line check).
  * ``evaluate_proposal`` — called by the weekly workflow on a structured ``OrderProposal``
    before staging (the primary, portfolio-aware check).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from agent.settings import Settings

BUY_SIDES = {"BUY", "BOT", "B"}
SELL_SIDES = {"SELL", "SLD", "S"}


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reason: str = ""
    breaches: tuple[str, ...] = ()


@dataclass(frozen=True)
class OrderProposal:
    """A structured trade proposal produced by the weekly review."""

    symbol: str
    side: str                 # "BUY" | "SELL"
    quantity: float
    limit_price: float        # price used to compute notional (last/limit)
    asset_class: str          # "stock" | "etf" | "option"
    sector: str = "UNKNOWN"
    stop_price: float | None = None
    rationale: str = ""

    @property
    def notional(self) -> float:
        mult = 100.0 if self.asset_class == "option" else 1.0
        return abs(self.quantity) * self.limit_price * mult


@dataclass(frozen=True)
class PortfolioState:
    """Snapshot of the account used for portfolio-aware checks."""

    equity: float                       # total account equity (net liq)
    cash: float                         # available cash
    position_values: dict[str, float] = field(default_factory=dict)  # symbol -> mkt value
    sector_values: dict[str, float] = field(default_factory=dict)    # sector -> mkt value


def _pct(part: float, whole: float) -> float:
    return 0.0 if whole <= 0 else (part / whole) * 100.0


def check_order_instruction(tool_input: dict[str, Any], settings: Settings) -> Verdict:
    """Defensive check on the raw ``create_order_instruction`` MCP payload.

    Field names follow the IBKR connector's order-instruction schema; we read defensively
    because payload shapes vary. This is a coarse notional/asset-class gate — the
    portfolio-aware limits are enforced earlier by ``evaluate_proposal``.
    """
    side = str(tool_input.get("side") or tool_input.get("action") or "").upper()
    asset_class = str(tool_input.get("asset_class") or tool_input.get("sec_type") or "stock").lower()

    if asset_class not in settings.asset_classes:
        return Verdict(False, f"asset class '{asset_class}' is not enabled in config.")

    qty = _coerce_float(tool_input.get("quantity") or tool_input.get("qty"))
    price = _coerce_float(
        tool_input.get("limit_price")
        or tool_input.get("price")
        or tool_input.get("aux_price")
    )
    if qty is None or price is None:
        # Can't evaluate notional — allow shape through only for SELL (risk-reducing);
        # block un-priced BUYs so we never stage an unbounded buy.
        if side in BUY_SIDES:
            return Verdict(False, "buy order missing quantity/price; cannot size-check.")
        return Verdict(True)

    mult = 100.0 if asset_class == "option" else 1.0
    notional = abs(qty) * price * mult
    if side in BUY_SIDES and notional > settings.risk.max_order_notional_usd:
        return Verdict(
            False,
            f"order notional ${notional:,.0f} exceeds cap "
            f"${settings.risk.max_order_notional_usd:,.0f}.",
        )
    return Verdict(True)


def evaluate_proposal(
    proposal: OrderProposal,
    portfolio: PortfolioState,
    settings: Settings,
) -> Verdict:
    """Portfolio-aware check on a structured proposal. Returns all breaches, not just the first."""
    r = settings.risk
    breaches: list[str] = []

    # SELLs reduce risk — only sanity-check them, don't cap.
    if proposal.side.upper() in SELL_SIDES:
        if proposal.quantity <= 0:
            breaches.append("sell quantity must be positive")
        return Verdict(not breaches, "; ".join(breaches), tuple(breaches))

    if proposal.asset_class not in settings.asset_classes:
        breaches.append(f"asset class '{proposal.asset_class}' not enabled")

    if proposal.quantity <= 0 or proposal.limit_price <= 0:
        breaches.append("quantity and price must be positive")

    # Every BUY entry must carry a loss-cutting stop (CAN SLIM defense).
    if proposal.stop_price is None:
        breaches.append("buy proposal has no stop-loss attached")

    notional = proposal.notional

    # Per-order notional cap.
    if notional > r.max_order_notional_usd:
        breaches.append(
            f"notional ${notional:,.0f} > per-order cap ${r.max_order_notional_usd:,.0f}"
        )

    # Cash buffer: buying must not drop cash below the buffer.
    min_cash = portfolio.equity * (r.cash_buffer_pct / 100.0)
    if portfolio.cash - notional < min_cash:
        breaches.append(
            f"would breach {r.cash_buffer_pct:.0f}% cash buffer "
            f"(cash ${portfolio.cash:,.0f} - ${notional:,.0f} < ${min_cash:,.0f})"
        )

    # Resulting position weight cap.
    existing = portfolio.position_values.get(proposal.symbol, 0.0)
    new_weight = _pct(existing + notional, portfolio.equity)
    if new_weight > r.max_position_weight_pct:
        breaches.append(
            f"{proposal.symbol} would be {new_weight:.1f}% of equity "
            f"> {r.max_position_weight_pct:.0f}% cap"
        )

    # Resulting sector weight cap.
    sec_existing = portfolio.sector_values.get(proposal.sector, 0.0)
    new_sec_weight = _pct(sec_existing + notional, portfolio.equity)
    if new_sec_weight > r.max_sector_weight_pct:
        breaches.append(
            f"sector '{proposal.sector}' would be {new_sec_weight:.1f}% "
            f"> {r.max_sector_weight_pct:.0f}% cap"
        )

    return Verdict(not breaches, "; ".join(breaches), tuple(breaches))


def filter_new_entries(
    proposals: Iterable[OrderProposal],
    portfolio: PortfolioState,
    settings: Settings,
) -> tuple[list[OrderProposal], list[tuple[OrderProposal, Verdict]]]:
    """Split BUY proposals into (accepted, rejected), honoring the max-new-positions cap.

    Proposals are evaluated in the order given (rank them upstream by CAN SLIM score), each
    against a portfolio state that is updated as accepted buys consume cash/weight — so the
    cap checks compound correctly across the batch.
    """
    accepted: list[OrderProposal] = []
    rejected: list[tuple[OrderProposal, Verdict]] = []
    max_new = settings.risk.max_new_positions_per_review

    # Mutable working copy of the portfolio.
    cash = portfolio.cash
    pos = dict(portfolio.position_values)
    sec = dict(portfolio.sector_values)

    new_positions_added = 0
    for p in proposals:
        working = PortfolioState(
            equity=portfolio.equity, cash=cash, position_values=pos, sector_values=sec
        )
        is_new_symbol = p.symbol not in pos or pos.get(p.symbol, 0.0) == 0.0

        verdict = evaluate_proposal(p, working, settings)
        if verdict.ok and is_new_symbol and new_positions_added >= max_new:
            verdict = Verdict(
                False,
                f"max new positions per review ({max_new}) reached",
                ("max_new_positions",),
            )

        if verdict.ok:
            accepted.append(p)
            cash -= p.notional
            pos[p.symbol] = pos.get(p.symbol, 0.0) + p.notional
            sec[p.sector] = sec.get(p.sector, 0.0) + p.notional
            if is_new_symbol:
                new_positions_added += 1
        else:
            rejected.append((p, verdict))

    return accepted, rejected


def _coerce_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
