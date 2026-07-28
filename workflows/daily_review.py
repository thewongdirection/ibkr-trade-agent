"""Daily pre-market review — the scheduled entry point for the hosted bot.

Runs once each morning (US pre-market via a Claude Routine, see docs/HOSTING.md). It:
  1. reads the account (positions, balances) and the market direction,
  2. grades every holding with CAN SLIM -> hold / trim / exit,
  3. gathers NEW ideas from two sources — the `can-slim-recommend` screen AND fresh
     candidates from the stock-movement-monitor signal feed — grades them, and
  4. sizes + risk-checks every buy, stages the survivors for your one-tap approval, and
  5. emits three deliverables: an HTML dashboard, a chat brief, and a short push payload.

Nothing executes automatically: orders are only ever *staged*. The live-data dependencies sit
behind ``DailyContext`` (backed by the IBKR/FMP connectors + CAN SLIM skills in a hosted run;
injected with fixtures in tests), marked ``TODO(connector)``.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from agent.runtime import mode_banner
from agent.settings import Settings, load_settings
from analysis import canslim
from analysis.canslim import Grade, ManagementAction, Recommendation, Verdict
from analysis.positions import Holding, build_portfolio_state, exposure_summary, parse_holdings
from journal.store import Journal
from reporting.dashboard import DashboardData, render_dashboard
from risk.guardrails import OrderProposal, PortfolioState, evaluate_proposal, filter_new_entries
from risk.sizing import size_entry
from signals.feed import Signal, SignalCandidate, to_candidates


def _needs_connector(what: str):
    raise RuntimeError(
        f"DailyContext.{what} is not wired to a connector. Run inside a Claude session with "
        "the IBKR/FMP MCP servers attached, or inject a fixture for a dry run/test."
    )


@dataclass
class DailyContext:
    """Live-data seam. Defaults raise so an un-wired live run fails loudly, never on empty data."""

    fetch_positions: Callable[[], list[dict[str, Any]]] = field(
        default=lambda: _needs_connector("fetch_positions"))
    fetch_balances: Callable[[], dict[str, Any]] = field(
        default=lambda: _needs_connector("fetch_balances"))
    fetch_sectors: Callable[[list[str]], dict[str, str]] = field(default=lambda symbols: {})
    assess_market: Callable[[], str] = field(default=lambda: "unknown")
    grade_symbol: Callable[[str], Grade] = field(
        default=lambda symbol: _needs_connector("grade_symbol"))
    recommend_new: Callable[[int], list[Recommendation]] = field(
        default=lambda count: _needs_connector("recommend_new"))
    load_signals: Callable[[], list[Signal]] = field(default=lambda: [])
    identity_verified: bool | None = None
    stage_order: Callable[[OrderProposal], None] = field(default=lambda proposal: None)
    now: datetime | None = None


@dataclass
class DailyResult:
    run_id: int
    mode: str
    market_read: str
    base_currency: str
    identity_verified: bool | None
    exposure: dict[str, Any]
    staged_live: bool = False
    management: list[dict[str, Any]] = field(default_factory=list)
    proposals: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)
    signal_candidates: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _grade_to_buy_idea(symbol: str, grade: Grade, sector: str, reason: str):
    """A signal/recommend candidate becomes a buy idea only if it grades BUY-RANGE with a pivot."""
    if grade.verdict is not Verdict.BUY_RANGE or not grade.pivot:
        return None
    return {"symbol": symbol, "price": grade.pivot, "stop": grade.stop,
            "sector": sector, "reason": reason}


def run_daily_review(settings: Settings, ctx: DailyContext, *, stage: bool = False) -> DailyResult:
    canslim.require_skills(settings)
    journal = Journal(settings.journal_db_path)
    now = ctx.now or datetime.now(timezone.utc)

    market_read = ctx.assess_market()

    raw_positions = ctx.fetch_positions()
    balances = ctx.fetch_balances()
    symbols = [str(p.get("symbol") or p.get("ticker") or "") for p in raw_positions]
    sectors = ctx.fetch_sectors([s for s in symbols if s])
    holdings: list[Holding] = parse_holdings(raw_positions, sectors)
    portfolio = build_portfolio_state(holdings, balances)
    exposure = exposure_summary(portfolio)

    run_id = journal.start_run(
        mode=settings.mode, market_read=market_read,
        equity=portfolio.equity, cash=portfolio.cash, notes="daily")

    result = DailyResult(
        run_id=run_id, mode=settings.mode, market_read=market_read,
        base_currency=settings.base_currency, identity_verified=ctx.identity_verified,
        exposure=exposure, staged_live=stage,
    )
    if ctx.identity_verified is False:
        result.notes.append("IDENTITY MISMATCH — connected account does not match your marker; "
                            "no orders staged.")

    held = {h.symbol for h in holdings}

    # 1 — Manage existing holdings.
    sells: list[OrderProposal] = []
    for h in holdings:
        grade = ctx.grade_symbol(h.symbol)
        journal.record_grade(run_id, h.symbol, grade.verdict.value, grade.score_out_of_70,
                             grade.letters, grade.summary)
        action, reason = canslim.decide_management_action(grade, h.unrealized_pnl_pct, h.dte, settings)
        result.management.append({
            "symbol": h.symbol, "quantity": h.quantity, "market_value": h.market_value,
            "pnl_pct": round(h.unrealized_pnl_pct, 1), "verdict": grade.verdict.value,
            "action": action.value, "reason": reason,
        })
        if action in (ManagementAction.EXIT, ManagementAction.TRIM):
            qty = h.quantity if action is ManagementAction.EXIT else round(h.quantity / 2)
            sells.append(OrderProposal(
                symbol=h.symbol, side="SELL", quantity=abs(qty),
                limit_price=(h.market_value / h.quantity) if h.quantity else 0.0,
                asset_class=h.asset_class, sector=h.sector, rationale=reason))
        else:
            journal.record_decision(run_id, h.symbol, "HOLD", "noted",
                                    sector=h.sector, rationale=reason)

    # 2 — Gather NEW ideas: CAN SLIM screen + monitor signal feed.
    buy_ideas: list[dict[str, Any]] = []

    for rec in ctx.recommend_new(settings.new_ideas_count):
        if rec.symbol in held:
            continue
        buy_ideas.append({"symbol": rec.symbol, "price": rec.buy_point or rec.price,
                          "stop": rec.stop, "sector": rec.sector, "reason": rec.reason})

    signal_candidates: list[SignalCandidate] = to_candidates(
        ctx.load_signals(), now=now, exclude=held)
    for cand in signal_candidates:
        grade = ctx.grade_symbol(cand.ticker)
        journal.record_grade(run_id, cand.ticker, grade.verdict.value, grade.score_out_of_70,
                             grade.letters, f"[signal] {cand.reason}")
        result.signal_candidates.append({
            "ticker": cand.ticker, "verdict": grade.verdict.value,
            "top_severity": cand.top_severity, "reason": cand.reason,
        })
        idea = _grade_to_buy_idea(cand.ticker, grade, "SIGNAL",
                                  f"{cand.reason}; CAN SLIM {grade.verdict.value}")
        if idea and idea["symbol"] not in {b["symbol"] for b in buy_ideas}:
            buy_ideas.append(idea)

    # 3 — Size + risk-check every buy.
    entries = [size_entry(portfolio=portfolio, settings=settings, **idea) for idea in buy_ideas]
    entries = [e for e in entries if e.quantity > 0]

    # Identity mismatch => propose nothing.
    if ctx.identity_verified is False:
        entries = []

    accepted_buys, rejected = filter_new_entries(entries, portfolio, settings)

    accepted: list[OrderProposal] = []
    for s in sells:
        v = evaluate_proposal(s, portfolio, settings)
        if v.ok:
            accepted.append(s)
        else:
            rejected.append((s, v))
    accepted += accepted_buys

    # An identity mismatch aborts ALL staging — no sells, no buys.
    if ctx.identity_verified is False:
        accepted = []

    # 4 — Stage (optional) + journal.
    for p in accepted:
        disposition = "staged" if stage else "noted"
        if stage:
            ctx.stage_order(p)  # TODO(connector): create_order_instruction via IBKR MCP
        journal.record_decision(
            run_id, p.symbol, p.side, disposition, quantity=p.quantity,
            limit_price=p.limit_price, stop_price=p.stop_price, notional=round(p.notional, 2),
            asset_class=p.asset_class, sector=p.sector, rationale=p.rationale)
        result.proposals.append({
            "symbol": p.symbol, "side": p.side, "quantity": p.quantity,
            "limit_price": p.limit_price, "stop": p.stop_price,
            "notional": round(p.notional, 2), "reason": p.rationale})

    for p, v in rejected:
        journal.record_decision(run_id, p.symbol, p.side, "rejected", quantity=p.quantity,
                                limit_price=p.limit_price, notional=round(p.notional, 2),
                                asset_class=p.asset_class, sector=p.sector,
                                rationale=p.rationale, reject_reason=v.reason)
        result.rejected.append({"symbol": p.symbol, "side": p.side, "reason": v.reason})

    if not portfolio.equity:
        result.notes.append("Account is unfunded — review is informational until it holds cash.")

    return result


# --------------------------------------------------------------------------- deliverables


def build_dashboard(result: DailyResult, settings: Settings, generated_at: str) -> str:
    data = DashboardData(
        generated_at=generated_at, mode=result.mode,
        identity_verified=result.identity_verified, market_read=result.market_read,
        base_currency=result.base_currency, equity=result.exposure.get("equity", 0.0),
        cash=result.exposure.get("cash", 0.0), cash_pct=result.exposure.get("cash_pct", 0.0),
        invested_pct=result.exposure.get("invested_pct", 0.0),
        positions=result.management, proposals=result.proposals, rejected=result.rejected,
        signal_candidates=result.signal_candidates, staged_live=result.staged_live,
        notes=result.notes,
    )
    return render_dashboard(data)


def chat_brief(result: DailyResult, settings: Settings) -> str:
    lines = [
        mode_banner(settings),
        f"Daily review #{result.run_id} — market: {result.market_read}",
        f"Equity {result.exposure.get('equity',0):,.0f} {result.base_currency} | "
        f"cash {result.exposure.get('cash_pct',0)}% | invested {result.exposure.get('invested_pct',0)}%",
    ]
    if result.management:
        lines.append("Holdings: " + ", ".join(
            f"{m['symbol']} {m['action']}" for m in result.management))
    verb = "STAGED (approve in IBKR)" if result.staged_live else "PROPOSED (not staged)"
    if result.proposals:
        lines.append(f"{verb}:")
        for p in result.proposals:
            lines.append(f"  {p['side']} {p['symbol']} {p['quantity']:g} @ {p['limit_price']} "
                         f"— {p['reason']}")
    else:
        lines.append("No orders proposed.")
    if result.signal_candidates:
        lines.append("Monitor candidates: " + ", ".join(
            f"{c['ticker']}({c['verdict']})" for c in result.signal_candidates))
    for n in result.notes:
        lines.append(f"! {n}")
    return "\n".join(lines)


def push_payload(result: DailyResult) -> dict[str, str]:
    """Short push alert: headline count of actions needing approval."""
    n_buy = sum(1 for p in result.proposals if p["side"] == "BUY")
    n_sell = sum(1 for p in result.proposals if p["side"] == "SELL")
    if result.identity_verified is False:
        title, body = "IBKR review halted", "Account identity mismatch — check the connector."
    elif not result.proposals:
        title = "IBKR daily review — no actions"
        body = f"Market {result.market_read}. Nothing to approve today."
    else:
        title = f"IBKR review — {len(result.proposals)} to approve"
        body = f"{n_buy} buy / {n_sell} sell staged. Market {result.market_read}."
    return {"title": title, "body": body}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the daily IBKR portfolio review.")
    parser.add_argument("--stage", action="store_true",
                        help="Stage accepted proposals for approval (default: dry run).")
    parser.add_argument("--out", default=None, help="Write the HTML dashboard to this path.")
    parser.add_argument("--config", default=None)
    args = parser.parse_args(argv)

    settings = load_settings(args.config)
    print(mode_banner(settings))

    ctx = DailyContext()  # TODO(connector): back with IBKR/FMP + skills + signal feed.
    try:
        result = run_daily_review(settings, ctx, stage=args.stage)
    except (RuntimeError, FileNotFoundError) as exc:
        print(f"\nCannot run end-to-end yet: {exc}", file=sys.stderr)
        return 2

    print(chat_brief(result, settings))
    if args.out:
        from pathlib import Path
        Path(args.out).write_text(build_dashboard(result, settings, "(cli run)"))
        print(f"\nDashboard written to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
