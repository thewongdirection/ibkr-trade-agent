# Integration with stock-movement-monitor

The [`stock-movement-monitor`](https://github.com/thewongdirection/stock-movement-monitor) and
this trade agent are **two halves of one system, kept as separate repos**:

- **monitor = the eyes** — a read-only surveillance bot that watches a watchlist and emits
  alerts (unusual volume, block/dark-pool prints, options flow, insider Form 4). No account,
  no trading.
- **trade agent = the hands** — manages your IBKR account, grades holdings and ideas with CAN
  SLIM, and stages trades for your approval.

They are **loosely coupled**: they never import each other. They meet only at a small,
versioned JSON **signal-feed contract**. The monitor *produces* it; the trade agent *consumes*
it as one more candidate source for the daily review.

```
  stock-movement-monitor                     ibkr-trade-agent
  ┌────────────────────┐   signal feed      ┌──────────────────────────┐
  │ detectors → Alerts │ ──(signals.json)──►│ signals.load_feed         │
  └────────────────────┘                    │ signals.to_candidates     │
                                            │   → CAN SLIM grade         │
                                            │   → risk-check → stage     │
                                            └──────────────────────────┘
```

## The contract

A JSON document. The `signals` array is the monitor's `Alert` records verbatim:

```json
{
  "version": 1,
  "source": "stock-movement-monitor",
  "generated_at": "2026-07-28T12:25:00Z",
  "signals": [
    {
      "ticker": "NVDA",
      "detector": "insider_trades",
      "severity": "high",
      "headline": "Director purchase — 12,000 sh ($2.1M)",
      "occurred_at": "2026-07-27T18:03:00Z",
      "url": "https://www.sec.gov/...",
      "dedup_id": "a1b2c3d4e5f6",
      "lines": ["Cluster buy: 2 insiders", "Filed 1 day after trade"]
    }
  ]
}
```

| Field | Required | Notes |
|---|---|---|
| `ticker` | yes | Uppercased on ingest |
| `detector` | yes | One of the monitor's detector names |
| `severity` | yes | `low` \| `medium` \| `high` |
| `headline` | no | Used to tell insider *buys* from *sales* |
| `occurred_at` | yes | ISO 8601; `Z` or offset accepted |
| `url`, `dedup_id`, `lines` | no | Passed through for context/dedup |

Unknown fields are ignored; a row missing `ticker` or a parseable `occurred_at` is skipped
(one bad row never sinks the feed).

## How the trade agent uses it

`signals.to_candidates()` keeps only signals that are:
- **fresh** (default: last 7 days),
- **material** (severity ≥ medium), and
- **bullish/accumulation** (accumulation detectors; insider rows filtered to *purchases*),

then groups them by ticker and ranks by **corroboration** (distinct detectors) → severity →
recency. A raw signal never becomes a trade: each candidate ticker is **CAN SLIM graded** and
**risk-checked** in the daily review, and only survivors are staged for your approval.

## Producing the feed (monitor side — not yet wired)

The consumer is built and tested here. The remaining piece is a small, additive exporter in
the monitor repo that writes its recent alerts to `signals.json` in this shape — e.g. a
`monitor export-signals --days 7 --out signals.json` command, published as a GitHub Actions
artifact / committed to a known path / dropped in shared storage the daily Routine can read.
That change lives in the monitor's repo and is intentionally left for a follow-up so the
mature monitor stays untouched until you approve it.

Until the exporter exists, point the trade agent at a hand-written or exported `signals.json`
via the daily review's signal-feed path (see `workflows/`).
