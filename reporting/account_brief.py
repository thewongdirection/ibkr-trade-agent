"""Concise, on-demand account & positions brief.

Distinct from two neighbours:
* :func:`broker.account.render_account` — the *verbose* multi-section CLI dump.
* :func:`workflows.daily_review.chat_brief` — the *review* brief (grades, staged orders).

This one answers a plain "how's my account right now?" in a few lines suitable for a chat or
Telegram reply: mode banner, equity/cash/invested, P&L, and the top positions. Read-only — it
takes a :class:`broker.client.BrokerClient` and formats what it returns; it never trades and
never prints the (connector-masked) account number or owner name.
"""

from __future__ import annotations

from agent.runtime import mode_banner
from agent.settings import Settings
from broker.client import BrokerClient


def _pct(part: float, whole: float) -> float:
    return 0.0 if whole == 0 else part / whole * 100.0


def account_brief(client: BrokerClient, settings: Settings, *, top: int = 8) -> str:
    """Return a short plain-text account summary. ``top`` caps positions listed."""
    ccy = settings.base_currency
    summary = client.account_summary()
    positions = sorted(client.positions(), key=lambda p: abs(p.market_value), reverse=True)

    equity = summary.net_liquidation
    cash = summary.total_cash_value
    invested = summary.gross_position_value or sum(p.market_value for p in positions)
    upnl = sum(p.unrealized_pnl for p in positions)

    lines = [mode_banner(settings)]

    if not summary.is_funded and not positions:
        lines.append("Account is unfunded — no equity or positions yet.")
        return "\n".join(lines)

    lines.append(
        f"Equity {equity:,.0f} {ccy}  |  cash {cash:,.0f} ({_pct(cash, equity):.0f}%)  |  "
        f"invested {invested:,.0f} ({_pct(invested, equity):.0f}%)"
    )
    sign = "+" if upnl >= 0 else ""
    lines.append(f"Open P&L: {sign}{upnl:,.0f} {ccy} across {len(positions)} position(s)")

    if positions:
        lines.append("")
        lines.append(f"Top positions (of {len(positions)}):")
        for p in positions[:top]:
            wt = _pct(p.market_value, equity)
            lines.append(
                f"  {p.symbol:<7} {p.quantity:>8,.0f} @ {p.price:,.2f}  "
                f"mv {p.market_value:,.0f}  {p.unrealized_pnl_pct:+.1f}%  ({wt:.0f}% wt)"
            )
        if len(positions) > top:
            lines.append(f"  … and {len(positions) - top} more")

    try:
        open_orders = client.orders()
        if open_orders:
            lines.append("")
            lines.append(f"Working orders ({len(open_orders)}):")
            for o in open_orders:
                lines.append(f"  {o.side} {o.symbol} {o.quantity:g} {o.order_type} @ {o.price:g}"
                             f"  [{o.status}]")
    except Exception:  # noqa: BLE001 - orders are a nice-to-have; never fail the brief on them
        pass

    return "\n".join(lines)


def positions_brief(client: BrokerClient, settings: Settings) -> str:
    """Just the positions table (every position, no truncation)."""
    ccy = settings.base_currency
    positions = sorted(client.positions(), key=lambda p: abs(p.market_value), reverse=True)
    if not positions:
        return f"{mode_banner(settings)}\nNo open positions."
    lines = [f"Positions ({len(positions)}) — {ccy}:"]
    for p in positions:
        lines.append(
            f"  {p.symbol:<7} {p.quantity:>8,.0f} @ {p.price:,.2f}  "
            f"mv {p.market_value:,.0f}  uPnL {p.unrealized_pnl:,.0f} ({p.unrealized_pnl_pct:+.1f}%)"
            f"  [{p.asset_class}]"
        )
    return "\n".join(lines)
