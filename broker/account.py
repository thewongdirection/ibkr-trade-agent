"""CLI to retrieve and display IBKR account information, trades, and history.

    python -m broker.account                 # summary + balances + positions + open orders
    python -m broker.account --trades DAYS_90 # + trade history for the given period
    python -m broker.account --all            # everything, including 90-day trade history

Read-only: this module never places, modifies, or cancels an order. It formats what the
connector returns; the account number and owner name are masked by the connector and are
therefore reported as such rather than displayed.
"""

from __future__ import annotations

import argparse
import sys

from agent.settings import Settings, load_settings
from broker.client import BrokerClient
from broker.session import format_status, verify_connection

TRADE_PERIODS = (
    "TODAY", "DAYS_7", "DAYS_30", "DAYS_60", "DAYS_90",
    "MONTH_TO_DATE", "YEAR_TO_DATE",
    "LAST_QUARTER", "TWO_QUARTERS_AGO", "THREE_QUARTERS_AGO", "FOUR_QUARTERS_AGO",
)


def render_account(
    client: BrokerClient,
    settings: Settings,
    trades_period: str | None = None,
) -> str:
    ccy = ""
    out: list[str] = []

    status = verify_connection(client, settings)
    out.append(format_status(status, settings))
    ccy = status.base_currency

    summary = client.account_summary()
    out.append("")
    out.append("=== Account summary ===")
    out.append(f"  Net liquidation : {summary.net_liquidation:,.2f} {ccy}")
    out.append(f"  Equity w/ loan  : {summary.equity_with_loan_value:,.2f} {ccy}")
    out.append(f"  Total cash      : {summary.total_cash_value:,.2f} {ccy}")
    out.append(f"  Available funds : {summary.available_funds:,.2f} {ccy}")
    out.append(f"  Buying power    : {summary.buying_power:,.2f} {ccy}")
    out.append(f"  Init/Maint margin: {summary.initial_margin:,.2f} / "
               f"{summary.maintenance_margin:,.2f} {ccy}")
    out.append(f"  Excess liquidity: {summary.excess_liquidity:,.2f} {ccy}")
    out.append(f"  Leverage        : {summary.leverage}")

    balances = client.balances()
    out.append("")
    out.append("=== Balances by currency ===")
    if not balances:
        out.append("  (none)")
    for b in balances:
        out.append(
            f"  {b.currency:<5} cash {b.cash_balance:,.2f} | mkt val "
            f"{b.stock_market_value:,.2f} | uPnL {b.unrealized_pnl:,.2f} | "
            f"fx {b.exchange_rate}"
        )

    positions = client.positions()
    out.append("")
    out.append(f"=== Open positions ({len(positions)}) ===")
    if not positions:
        out.append("  (no open positions)")
    for p in positions:
        out.append(
            f"  {p.symbol:<8} {p.quantity:>10,.2f} @ {p.price:,.2f}  "
            f"mkt {p.market_value:,.2f}  uPnL {p.unrealized_pnl:,.2f} "
            f"({p.unrealized_pnl_pct:+.1f}%)  [{p.asset_class}]"
        )

    orders = client.orders()
    out.append("")
    out.append(f"=== Open orders ({len(orders)}) ===")
    if not orders:
        out.append("  (no working orders)")
    for o in orders:
        out.append(
            f"  {o.symbol:<8} {o.side:<4} {o.quantity:>8,.2f} {o.order_type:<6} "
            f"@ {o.price:,.2f}  {o.status}  filled {o.filled:,.2f}  #{o.order_id}"
        )

    if trades_period:
        trades = client.trades(period=trades_period)
        out.append("")
        out.append(f"=== Trade history ({trades_period}, {len(trades)} trades) ===")
        if not trades:
            out.append("  (no trades in this period)")
        for t in trades:
            out.append(
                f"  {t.trade_time:<20} {t.symbol:<8} {t.side:<4} {t.size:>8,.2f} "
                f"@ {t.price:,.2f}  comm {t.commission:,.2f} {t.currency}  #{t.trade_id}"
            )

    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Retrieve IBKR account info, trades, history.")
    parser.add_argument(
        "--trades", metavar="PERIOD", choices=TRADE_PERIODS, default=None,
        help="Include trade history for the given period (e.g. DAYS_90, YEAR_TO_DATE).",
    )
    parser.add_argument(
        "--all", action="store_true",
        help="Show everything including 90-day trade history.",
    )
    parser.add_argument("--config", default=None, help="Path to config.yaml.")
    args = parser.parse_args(argv)

    settings = load_settings(args.config)
    trades_period = "DAYS_90" if args.all else args.trades

    from agent.runtime import build_broker_client

    try:
        client = build_broker_client(settings)
        report = render_account(client, settings, trades_period)
    except Exception as exc:  # noqa: BLE001 - surface connector/transport errors cleanly
        print(f"Could not retrieve account: {exc}", file=sys.stderr)
        print(
            "\nRun inside a Claude session with the IBKR connector attached, or bind an "
            "Agent SDK MCP client in build_broker_client() (see TODO(connector)).",
            file=sys.stderr,
        )
        return 2

    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
