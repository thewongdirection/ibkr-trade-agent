"""Self-contained daily HTML dashboard.

Rendered server-side in Python (no JavaScript, no external assets) so it is trivially
testable and opens directly in any browser. Dark "instrument-panel" theme with a light
fallback via ``prefers-color-scheme``. The whole page is built from a ``DashboardData``
struct the daily review fills.

Privacy: the dashboard shows balances and holdings (it is your private ops report), but never
the account number or owner name — those are masked by the connector anyway, and the header
says so. Do not publish this file to a shared/hosted location.
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field
from typing import Any


@dataclass
class DashboardData:
    generated_at: str
    mode: str                      # "paper" | "live"
    identity_verified: bool | None
    market_read: str
    base_currency: str
    equity: float
    cash: float
    cash_pct: float
    invested_pct: float
    positions: list[dict[str, Any]] = field(default_factory=list)   # holding rows w/ grade+action
    proposals: list[dict[str, Any]] = field(default_factory=list)   # staged/proposed orders
    rejected: list[dict[str, Any]] = field(default_factory=list)    # risk-rejected
    signal_candidates: list[dict[str, Any]] = field(default_factory=list)
    staged_live: bool = False
    notes: list[str] = field(default_factory=list)


def _esc(v: Any) -> str:
    return html.escape(str(v), quote=True)


def _money(v: float, ccy: str) -> str:
    try:
        return f"{float(v):,.2f} {ccy}"
    except (TypeError, ValueError):
        return f"0.00 {ccy}"


def _action_class(action: str) -> str:
    return {
        "EXIT": "bad", "TRIM": "warn", "HOLD": "ok",
        "BUY": "buy", "SELL": "warn",
    }.get(str(action).upper(), "")


def _verdict_class(v: str) -> str:
    return {"BUY-RANGE": "ok", "WATCH": "warn", "AVOID": "bad"}.get(str(v).upper(), "")


def render_dashboard(d: DashboardData, title: str = "Daily IBKR Review") -> str:
    ccy = d.base_currency or ""
    mode_badge = "LIVE" if d.mode == "live" else "PAPER"
    mode_cls = "bad" if d.mode == "live" else "ok"
    ident = (
        "VERIFIED" if d.identity_verified is True
        else "MISMATCH" if d.identity_verified is False
        else "unverified"
    )
    ident_cls = "ok" if d.identity_verified is True else "bad" if d.identity_verified is False else "warn"

    verb = "Staged for approval" if d.staged_live else "Proposed (not staged)"

    parts: list[str] = []
    parts.append(_HEAD.replace("__TITLE__", _esc(title)))
    parts.append('<div class="wrap">')

    # Header
    parts.append(f"""
    <header>
      <h1>{_esc(title)}</h1>
      <div class="badges">
        <span class="badge {mode_cls}">{mode_badge}</span>
        <span class="badge {ident_cls}">identity: {ident}</span>
        <span class="badge">market: {_esc(d.market_read)}</span>
      </div>
      <div class="sub">Generated {_esc(d.generated_at)} &middot; account number &amp; owner masked by connector</div>
    </header>
    """)

    # Account tiles
    parts.append('<section class="tiles">')
    for label, val in [
        ("Equity", _money(d.equity, ccy)),
        ("Cash", f"{_money(d.cash, ccy)} ({d.cash_pct:.0f}%)"),
        ("Invested", f"{d.invested_pct:.0f}%"),
        ("Open positions", str(len(d.positions))),
    ]:
        parts.append(f'<div class="tile"><div class="tl">{_esc(label)}</div>'
                     f'<div class="tv">{_esc(val)}</div></div>')
    parts.append('</section>')

    # Positions
    parts.append('<section><h2>Positions &mdash; CAN SLIM review</h2>')
    if not d.positions:
        parts.append('<p class="empty">No open positions.</p>')
    else:
        rows = []
        for p in d.positions:
            rows.append(f"""
            <tr>
              <td class="sym">{_esc(p.get('symbol',''))}</td>
              <td class="num">{_esc(p.get('quantity',''))}</td>
              <td class="num">{_money(p.get('market_value',0), ccy)}</td>
              <td class="num {'ok' if _num(p.get('pnl_pct'))>=0 else 'bad'}">{_pct(p.get('pnl_pct'))}</td>
              <td><span class="pill {_verdict_class(p.get('verdict',''))}">{_esc(p.get('verdict','n/a'))}</span></td>
              <td><span class="pill {_action_class(p.get('action',''))}">{_esc(p.get('action',''))}</span></td>
              <td class="reason">{_esc(p.get('reason',''))}</td>
            </tr>""")
        parts.append(_table(["Symbol", "Qty", "Mkt value", "P&L", "Grade", "Action", "Why"], rows))
    parts.append('</section>')

    # Proposals
    parts.append(f'<section><h2>Orders &mdash; {_esc(verb)}</h2>')
    if not d.proposals:
        parts.append('<p class="empty">No orders proposed this run.</p>')
    else:
        rows = []
        for o in d.proposals:
            rows.append(f"""
            <tr>
              <td><span class="pill {_action_class(o.get('side',''))}">{_esc(o.get('side',''))}</span></td>
              <td class="sym">{_esc(o.get('symbol',''))}</td>
              <td class="num">{_esc(o.get('quantity',''))}</td>
              <td class="num">{_esc(o.get('limit_price',''))}</td>
              <td class="num">{_esc(o.get('stop','') if o.get('stop') is not None else '—')}</td>
              <td class="num">{_money(o.get('notional',0), ccy)}</td>
              <td class="reason">{_esc(o.get('reason',''))}</td>
            </tr>""")
        parts.append(_table(["Side", "Symbol", "Qty", "Limit", "Stop", "Notional", "Why"], rows))
    parts.append('</section>')

    # Signal candidates (from the monitor)
    if d.signal_candidates:
        parts.append('<section><h2>Monitor signals &mdash; new candidates</h2>')
        rows = []
        for c in d.signal_candidates:
            rows.append(f"""
            <tr>
              <td class="sym">{_esc(c.get('ticker',''))}</td>
              <td><span class="pill {_verdict_class(c.get('verdict',''))}">{_esc(c.get('verdict','ungraded'))}</span></td>
              <td class="num">{_esc(c.get('top_severity',''))}</td>
              <td class="reason">{_esc(c.get('reason',''))}</td>
            </tr>""")
        parts.append(_table(["Ticker", "CAN SLIM", "Severity", "Signal"], rows))
        parts.append('</section>')

    # Rejected
    if d.rejected:
        parts.append('<section><h2>Rejected by risk caps</h2>')
        rows = [f'<tr><td class="sym">{_esc(r.get("symbol",""))}</td>'
                f'<td>{_esc(r.get("side",""))}</td>'
                f'<td class="reason">{_esc(r.get("reason",""))}</td></tr>' for r in d.rejected]
        parts.append(_table(["Symbol", "Side", "Reason"], rows))
        parts.append('</section>')

    # Notes / disclaimer
    if d.notes:
        parts.append('<section class="notes"><h2>Notes</h2><ul>')
        for n in d.notes:
            parts.append(f'<li>{_esc(n)}</li>')
        parts.append('</ul></section>')

    parts.append(f"""
    <footer>
      Decision support only &mdash; not investment advice. Every order is staged for your
      one-tap approval; nothing executes automatically. CAN SLIM is a probability edge, not a
      guarantee. Loss-cutting stops are attached to every entry. Mode: {mode_badge}.
    </footer>
    """)

    parts.append('</div></body></html>')
    return "".join(parts)


def _table(headers: list[str], rows: list[str]) -> str:
    head = "".join(f"<th>{_esc(h)}</th>" for h in headers)
    return f'<div class="tablewrap"><table><thead><tr>{head}</tr></thead><tbody>{"".join(rows)}</tbody></table></div>'


def _num(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _pct(v: Any) -> str:
    try:
        return f"{float(v):+.1f}%"
    except (TypeError, ValueError):
        return "n/a"


_HEAD = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root{--bg:#0d1117;--panel:#161b22;--line:#232a33;--txt:#e6edf3;--dim:#8b949e;
--ok:#2ea043;--warn:#d29922;--bad:#f85149;--buy:#388bfd;--accent:#58a6ff;}
@media (prefers-color-scheme:light){:root{--bg:#f6f8fa;--panel:#fff;--line:#d0d7de;
--txt:#1f2328;--dim:#656d76;}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--txt);
font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;}
.wrap{max-width:1100px;margin:0 auto;padding:24px 18px 60px;}
header h1{margin:0 0 8px;font-size:22px;}
.badges{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:6px;}
.badge{border:1px solid var(--line);border-radius:999px;padding:2px 10px;font-size:12px;
background:var(--panel);color:var(--dim);}
.badge.ok{color:var(--ok);border-color:var(--ok)}.badge.warn{color:var(--warn);border-color:var(--warn)}
.badge.bad{color:var(--bad);border-color:var(--bad)}
.sub{color:var(--dim);font-size:12px;margin-bottom:18px;}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:8px;}
.tile{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px 14px;}
.tl{color:var(--dim);font-size:12px;text-transform:uppercase;letter-spacing:.04em;}
.tv{font-size:18px;font-weight:600;margin-top:4px;}
section{margin-top:26px;}h2{font-size:15px;border-bottom:1px solid var(--line);padding-bottom:6px;}
.tablewrap{overflow-x:auto;}table{width:100%;border-collapse:collapse;font-size:13px;}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);white-space:nowrap;}
th{color:var(--dim);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.03em;}
td.num{text-align:right;font-variant-numeric:tabular-nums;}td.sym{font-weight:600;}
td.reason{white-space:normal;color:var(--dim);min-width:220px;}
td.ok{color:var(--ok)}td.bad{color:var(--bad)}
.pill{display:inline-block;border-radius:6px;padding:2px 8px;font-size:12px;font-weight:600;
background:var(--line);color:var(--txt);}
.pill.ok{background:rgba(46,160,67,.18);color:var(--ok)}
.pill.warn{background:rgba(210,153,34,.18);color:var(--warn)}
.pill.bad{background:rgba(248,81,73,.18);color:var(--bad)}
.pill.buy{background:rgba(56,139,253,.18);color:var(--buy)}
.empty{color:var(--dim);}
.notes ul{margin:8px 0 0;padding-left:18px;color:var(--dim);}
footer{margin-top:34px;padding-top:14px;border-top:1px solid var(--line);color:var(--dim);font-size:12px;}
</style></head><body>"""
