"""Ingest the stock-movement-monitor signal feed and turn it into daily-review candidates.

Contract (see docs/INTEGRATION.md): the monitor publishes a JSON document whose ``signals``
array is a list of its ``Alert`` records — the fields are exactly the monitor's model:
``ticker, detector, severity, headline, occurred_at, url, dedup_id``. This module reads that
feed, keeps only *fresh, bullish, material* signals, and groups them by ticker into ranked
``SignalCandidate``s. The trade agent then grades each candidate with CAN SLIM and risk-checks
it before anything is staged — so a raw monitor signal never becomes a trade on its own; it
only earns a ticker a place in the grading queue.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

# Contract version the trade agent understands. The monitor stamps the feed with its own.
SIGNAL_FEED_VERSION = 1

_SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2}

# Detectors that indicate *accumulation / upside interest* — the only ones that make a name a
# BUY candidate. Side inference is unreliable on the tape (per the monitor's own caveats), so
# this is a coarse pre-filter; CAN SLIM grading is the real gate. Insider trades are included
# but further filtered to purchases in `_is_bullish`.
_ACCUMULATION_DETECTORS = {
    "volume_anomaly",
    "option_volume",
    "options_flow",
    "block_trades",
    "dark_pool",
    "insider_trades",
}

# Substrings in an insider headline/line that mark a SALE rather than a purchase. Insider
# alerts cover both; only purchases are bullish. The monitor's headline names the action.
_INSIDER_SELL_MARKERS = ("sale", "sold", "disposed", "sell")
_INSIDER_BUY_MARKERS = ("purchase", "bought", "buy", "acquired", "cluster buy")


@dataclass(frozen=True)
class Signal:
    """One monitor alert, as consumed by the trade agent."""

    ticker: str
    detector: str
    severity: str
    headline: str
    occurred_at: datetime
    url: str | None = None
    dedup_id: str = ""
    lines: tuple[str, ...] = ()
    source: str = "stock-movement-monitor"

    @property
    def severity_rank(self) -> int:
        return _SEVERITY_RANK.get(self.severity.lower(), 0)


@dataclass(frozen=True)
class SignalCandidate:
    """A ticker surfaced by one or more fresh bullish signals — a grading candidate."""

    ticker: str
    signal_count: int
    top_severity: str
    detectors: tuple[str, ...]
    latest: datetime
    reason: str
    signals: tuple[Signal, ...] = field(default_factory=tuple, repr=False)


def _parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        # Fall back to a bare date.
        try:
            dt = datetime.fromisoformat(text[:10])
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def load_feed(data: str | Path | dict[str, Any]) -> list[Signal]:
    """Load signals from a feed dict, a JSON string, or a path to a JSON file.

    Tolerant of missing optional fields; a record without a ticker or a parseable timestamp
    is skipped rather than raising, so one malformed row never sinks the feed.
    """
    if isinstance(data, (str, Path)) and _looks_like_path(data):
        raw = json.loads(Path(data).read_text())
    elif isinstance(data, str):
        raw = json.loads(data)
    else:
        raw = data

    records = raw.get("signals", []) if isinstance(raw, dict) else (raw or [])
    signals: list[Signal] = []
    for r in records:
        ticker = str(r.get("ticker") or "").upper().strip()
        occurred = _parse_dt(r.get("occurred_at") or r.get("occurredAt") or r.get("date"))
        if not ticker or occurred is None:
            continue
        lines = r.get("lines") or ()
        signals.append(
            Signal(
                ticker=ticker,
                detector=str(r.get("detector") or "").strip(),
                severity=str(r.get("severity") or "low").lower(),
                headline=str(r.get("headline") or ""),
                occurred_at=occurred,
                url=r.get("url"),
                dedup_id=str(r.get("dedup_id") or r.get("dedupId") or ""),
                lines=tuple(str(x) for x in lines),
                source=str(r.get("source") or "stock-movement-monitor"),
            )
        )
    return signals


def _looks_like_path(value: str | Path) -> bool:
    if isinstance(value, Path):
        return True
    s = value.strip()
    # A JSON payload starts with { or [; anything else short enough is treated as a path.
    return not (s.startswith("{") or s.startswith("["))


def _is_bullish(sig: Signal) -> bool:
    if sig.detector not in _ACCUMULATION_DETECTORS:
        return False
    if sig.detector == "insider_trades":
        text = (sig.headline + " " + " ".join(sig.lines)).lower()
        if any(m in text for m in _INSIDER_SELL_MARKERS) and not any(
            m in text for m in _INSIDER_BUY_MARKERS
        ):
            return False
    return True


def to_candidates(
    signals: Iterable[Signal],
    *,
    since_days: int = 7,
    min_severity: str = "medium",
    bullish_only: bool = True,
    now: datetime | None = None,
    exclude: Iterable[str] = (),
) -> list[SignalCandidate]:
    """Filter to fresh/material/bullish signals and group by ticker into ranked candidates.

    Ranking: more distinct detectors first (corroboration across independent signals is the
    strongest tell), then higher top severity, then more recent. ``exclude`` drops tickers you
    already hold if you only want *new* ideas from the feed.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=since_days)
    min_rank = _SEVERITY_RANK.get(min_severity.lower(), 1)
    excluded = {t.upper() for t in exclude}

    kept: dict[str, list[Signal]] = {}
    for s in signals:
        if s.ticker in excluded:
            continue
        if s.occurred_at < cutoff:
            continue
        if s.severity_rank < min_rank:
            continue
        if bullish_only and not _is_bullish(s):
            continue
        kept.setdefault(s.ticker, []).append(s)

    candidates: list[SignalCandidate] = []
    for ticker, sigs in kept.items():
        detectors = tuple(sorted({s.detector for s in sigs}))
        top = max(sigs, key=lambda s: s.severity_rank)
        latest = max(s.occurred_at for s in sigs)
        candidates.append(
            SignalCandidate(
                ticker=ticker,
                signal_count=len(sigs),
                top_severity=top.severity,
                detectors=detectors,
                latest=latest,
                reason=_reason(detectors, sigs),
                signals=tuple(sorted(sigs, key=lambda s: s.occurred_at, reverse=True)),
            )
        )

    candidates.sort(
        key=lambda c: (len(c.detectors), _SEVERITY_RANK.get(c.top_severity, 0), c.latest),
        reverse=True,
    )
    return candidates


def _reason(detectors: tuple[str, ...], sigs: list[Signal]) -> str:
    labels = {
        "volume_anomaly": "unusual volume",
        "option_volume": "unusual option volume",
        "options_flow": "bullish options flow",
        "block_trades": "large block prints",
        "dark_pool": "dark-pool accumulation",
        "insider_trades": "insider buying",
    }
    named = ", ".join(labels.get(d, d) for d in detectors)
    n = len(sigs)
    corroborated = " (corroborated across independent signals)" if len(detectors) > 1 else ""
    return f"{n} monitor signal{'s' if n != 1 else ''}: {named}{corroborated}"


def main(argv: list[str] | None = None) -> int:
    """Preview candidates from a feed file: `python -m signals.feed <feed.json>`."""
    import sys

    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print("usage: python -m signals.feed <feed.json>", file=sys.stderr)
        return 2
    signals = load_feed(Path(argv[0]))
    candidates = to_candidates(signals)
    print(f"{len(signals)} signals -> {len(candidates)} candidate ticker(s):")
    for c in candidates:
        print(f"  {c.ticker:<6} [{c.top_severity:<6}] {c.reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
