"""Weekly portfolio review — the scheduled entry point.

Point a Routine / cron entry at ``python -m workflows.weekly_review`` (see config.yaml →
schedule). By default it runs the review and prints the brief WITHOUT staging anything; pass
``--stage`` to actually stage the accepted proposals as IBKR order instructions for your
one-click approval.

The orchestration is real and deterministic: it opens a journal run, computes portfolio
state, applies the risk layer, and records every decision. The steps that need a live MCP
session — fetching positions/balances and invoking the CAN SLIM skills through the Agent SDK
— are isolated behind the ``ReviewContext`` seam and marked ``TODO(connector)`` so the whole
flow can be exercised with injected data in tests and dry runs.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from typing import Any, Callable

from agent.runtime import mode_banner
from agent.settings import Settings, load_settings
from analysis import canslim
from analysis.canslim import Grade, ManagementAction, Recommendation, Verdict
from analysis.positions import (
    Holding,
    build_portfolio_state,
    exposure_summary,
    parse_holdings,
)
from journal.store import Journal
from risk.guardrails import (
    OrderProposal,
    PortfolioState,
    evaluate_proposal,
    filter_new_entries,
)
from risk.sizing import size_entry


@dataclass
class ReviewContext:
    """Seam for the live-data dependencies.

    In production the callables here are backed by the Agent SDK loop with the IBKR + FMP MCP
    connectors. In tests / dry runs they are injected with fixtures, so the entire review can
    run without a connector. Defaults raise, making an un-wired live run fail loudly rather
    than silently trade on empty data.
    """

    fetch_positions: Callable[[], list[dict[str, Any]]] = field(
        default=lambda: _needs_connector("fetch_positions")
    )
    fetch_balances: Callable[[], dict[str, Any]] = field(
        default=lambda: _needs_connector("fetch_balances")
    )
    fetch_sectors: Callable[[list[str]], dict[str, str]] = field(
        default=lambda symbols: {}
    )
    assess_market: Callable[[], str] = field(default=lambda: "unknown")
    grade_symbol: Callable[[str], Grade] = field(
        default=lambda symbol: _needs_connector("grade_symbol")
    )
    recommend_new: Callable[[int], list[Recommendation]] = field(
        default=lambda count: _needs_connector("recommend_new")
    )
    stage_order: Callable[[OrderProposal], None] = field(default=lambda proposal: None)


def _needs_connector(what: str):
    raise RuntimeError(
        f"ReviewContext.{what} is not wired to a connector. "
        "Run inside a Claude session with the IBKR/FMP MCP servers attached, or inject a "
        "fixture for a dry run/test."
    )


@dataclass
class ReviewResult:
    run_id: int
    mode: str
    market_read: str
    exposure: dict[str, Any]
    staged_live: bool = False  # True only when orders were actually staged (--stage)
    management: list[dict[str, Any]] = field(default_factory=list)
    staged: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)


def _size_entry(rec: Recommendation, portfolio: PortfolioState, settings: Settings) -> OrderProposal:
    """Size a new entry to the per-order notional cap and remaining cash buffer."""
    return size_entry(
        symbol=rec.symbol,
        price=rec.buy_point or rec.price,
        stop=rec.stop,
        sector=rec.sector,
        reason=rec.reason,
        portfolio=portfolio,
        settings=settings,
    )


def run_review(
    settings: Settings,
    ctx: ReviewContext,
    *,
    stage: bool = False,
) -> ReviewResult:
    """Execute one weekly review. Stages orders only when ``stage=True``."""
    canslim.require_skills(settings)
    journal = Journal(settings.journal_db_path)

    # 1 — Market direction (M) first.
    market_read = ctx.assess_market()

    # 2 — Read the account.
    raw_positions = ctx.fetch_positions()
    balances = ctx.fetch_balances()
    symbols = [str(p.get("symbol") or p.get("ticker") or "") for p in raw_positions]
    sectors = ctx.fetch_sectors([s for s in symbols if s])
    holdings: list[Holding] = parse_holdings(raw_positions, sectors)
    portfolio = build_portfolio_state(holdings, balances)
    exposure = exposure_summary(portfolio)

    run_id = journal.start_run(
        mode=settings.mode,
        market_read=market_read,
        equity=portfolio.equity,
        cash=portfolio.cash,
        notes=f"style={settings.strategy_style}",
    )

    result = ReviewResult(
        run_id=run_id,
        mode=settings.mode,
        market_read=market_read,
        exposure=exposure,
        staged_live=stage,
    )

    # 3 — Manage existing holdings: grade each, decide HOLD/TRIM/EXIT.
    exit_and_trim: list[OrderProposal] = []
    for h in holdings:
        grade = ctx.grade_symbol(h.symbol)
        journal.record_grade(
            run_id, h.symbol, grade.verdict.value, grade.score_out_of_70,
            grade.letters, grade.summary,
        )
        action, reason = canslim.decide_management_action(
            grade, h.unrealized_pnl_pct, h.dte, settings
        )
        result.management.append(
            {"symbol": h.symbol, "action": action.value, "verdict": grade.verdict.value,
             "pnl_pct": round(h.unrealized_pnl_pct, 1), "reason": reason}
        )
        if action in (ManagementAction.EXIT, ManagementAction.TRIM):
            qty = h.quantity if action is ManagementAction.EXIT else round(h.quantity / 2)
            exit_and_trim.append(
                OrderProposal(
                    symbol=h.symbol, side="SELL", quantity=abs(qty),
                    limit_price=(h.market_value / h.quantity) if h.quantity else 0.0,
                    asset_class=h.asset_class, sector=h.sector, rationale=reason,
                )
            )
        else:
            journal.record_decision(
                run_id, h.symbol, "HOLD", "noted", sector=h.sector, rationale=reason
            )

    # 4 — Hunt new ideas.
    recs = ctx.recommend_new(settings.new_ideas_count)
    entry_proposals = [_size_entry(r, portfolio, settings) for r in recs]
    entry_proposals = [p for p in entry_proposals if p.quantity > 0]

    # 5 — Apply risk caps. SELLs (exits/trims) pass through; BUYs are filtered against caps.
    accepted_entries, rejected_entries = filter_new_entries(entry_proposals, portfolio, settings)

    accepted = list(exit_and_trim)
    for p in exit_and_trim:
        v = evaluate_proposal(p, portfolio, settings)  # sanity-check sells too
        if not v.ok:
            accepted.remove(p)
            rejected_entries.append((p, v))
    accepted += accepted_entries

    # 6 + 7 — Stage (optional) and journal every decision.
    for p in accepted:
        disposition = "staged" if stage else "noted"
        if stage:
            ctx.stage_order(p)  # TODO(connector): create_order_instruction via IBKR MCP
        journal.record_decision(
            run_id, p.symbol, p.side, disposition,
            quantity=p.quantity, limit_price=p.limit_price, stop_price=p.stop_price,
            notional=round(p.notional, 2), asset_class=p.asset_class, sector=p.sector,
            rationale=p.rationale,
        )
        result.staged.append(
            {"symbol": p.symbol, "side": p.side, "quantity": p.quantity,
             "limit_price": p.limit_price, "stop": p.stop_price,
             "notional": round(p.notional, 2), "reason": p.rationale}
        )

    for p, v in rejected_entries:
        journal.record_decision(
            run_id, p.symbol, p.side, "rejected",
            quantity=p.quantity, limit_price=p.limit_price, notional=round(p.notional, 2),
            asset_class=p.asset_class, sector=p.sector, rationale=p.rationale,
            reject_reason=v.reason,
        )
        result.rejected.append({"symbol": p.symbol, "side": p.side, "reason": v.reason})

    return result


def format_brief(result: ReviewResult, settings: Settings) -> str:
    """A short human brief for the chat/console."""
    lines = [
        mode_banner(settings),
        f"Weekly review #{result.run_id} — market: {result.market_read}",
        f"Equity {result.exposure.get('equity', 0):,.0f} {settings.base_currency} | "
        f"cash {result.exposure.get('cash_pct', 0)}% | "
        f"invested {result.exposure.get('invested_pct', 0)}%",
        "",
        "Existing positions:",
    ]
    for m in result.management or [{"symbol": "(none)", "action": "-", "reason": ""}]:
        lines.append(f"  {m['symbol']:<6} {m['action']:<5} {m.get('reason', '')}")
    lines.append("")
    verb = "STAGED for approval" if result.staged_live else "PROPOSED (not staged)"
    lines.append(f"{verb}:")
    for s in result.staged or [{"symbol": "(none)", "side": "", "reason": ""}]:
        note = f"{s.get('side','')} {s.get('quantity','')}x @ {s.get('limit_price','')}".strip()
        lines.append(f"  {s['symbol']:<6} {note}  — {s.get('reason','')}")
    if result.rejected:
        lines.append("")
        lines.append("Rejected by risk caps:")
        for rj in result.rejected:
            lines.append(f"  {rj['symbol']:<6} {rj['reason']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the weekly IBKR portfolio review.")
    parser.add_argument(
        "--stage", action="store_true",
        help="Stage accepted proposals as IBKR order instructions for one-click approval. "
             "Without this flag, nothing is staged (dry run).",
    )
    parser.add_argument("--config", default=None, help="Path to config.yaml.")
    args = parser.parse_args(argv)

    settings = load_settings(args.config)
    print(mode_banner(settings))

    # TODO(connector): construct a ReviewContext backed by the Agent SDK + IBKR/FMP MCP.
    # Until then, a live run fails loudly at the fetch step (by design), so guide the user.
    ctx = ReviewContext()
    try:
        result = run_review(settings, ctx, stage=args.stage)
    except (RuntimeError, FileNotFoundError) as exc:
        print(f"\nCannot run end-to-end yet: {exc}", file=sys.stderr)
        print(
            "This scaffold runs the full review logic once a ReviewContext is wired to the "
            "IBKR/FMP MCP connectors. See TODO(connector) in workflows/weekly_review.py.",
            file=sys.stderr,
        )
        return 2

    print(format_brief(result, settings))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
