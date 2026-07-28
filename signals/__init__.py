"""Integration with the stock-movement-monitor (loose coupling).

The monitor (https://github.com/thewongdirection/stock-movement-monitor) is a separate,
read-only market-surveillance bot: it watches a watchlist and emits alerts (unusual volume,
block/dark-pool prints, options flow, insider Form 4). This package lets the trade agent
*consume* those alerts as an extra candidate source for the daily review — the monitor is the
eyes, the trade agent is the hands. The two stay independent repos; they meet only at the
documented signal-feed contract (see docs/INTEGRATION.md and signals/feed.py).
"""

from signals.feed import (
    Signal,
    SignalCandidate,
    load_feed,
    to_candidates,
)

__all__ = ["Signal", "SignalCandidate", "load_feed", "to_candidates"]
