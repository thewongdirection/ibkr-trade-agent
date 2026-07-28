"""Tests for the stock-movement-monitor signal integration."""

from datetime import datetime, timezone

from signals.feed import Signal, load_feed, to_candidates

NOW = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)


def _sig(ticker, detector, severity, days_ago, headline="", lines=()):
    from datetime import timedelta

    return {
        "ticker": ticker,
        "detector": detector,
        "severity": severity,
        "headline": headline,
        "occurred_at": (NOW - timedelta(days=days_ago)).isoformat(),
        "lines": list(lines),
    }


def test_load_feed_from_dict_and_skips_bad_rows():
    feed = {
        "version": 1,
        "signals": [
            _sig("NVDA", "volume_anomaly", "high", 1),
            {"detector": "volume_anomaly", "severity": "high"},   # no ticker -> skip
            {"ticker": "BAD", "occurred_at": "not-a-date"},        # bad ts -> skip
        ],
    }
    signals = load_feed(feed)
    assert len(signals) == 1
    assert signals[0].ticker == "NVDA"


def test_load_feed_from_json_string():
    import json

    payload = json.dumps({"signals": [_sig("AMD", "options_flow", "medium", 0)]})
    assert load_feed(payload)[0].ticker == "AMD"


def test_freshness_filter():
    signals = load_feed({"signals": [
        _sig("OLD", "volume_anomaly", "high", 30),
        _sig("NEW", "volume_anomaly", "high", 2),
    ]})
    cands = to_candidates(signals, since_days=7, now=NOW)
    tickers = {c.ticker for c in cands}
    assert tickers == {"NEW"}


def test_severity_filter():
    signals = load_feed({"signals": [
        _sig("LOWSEV", "volume_anomaly", "low", 1),
        _sig("MIDSEV", "volume_anomaly", "medium", 1),
    ]})
    cands = to_candidates(signals, min_severity="medium", now=NOW)
    assert {c.ticker for c in cands} == {"MIDSEV"}


def test_insider_sale_excluded_purchase_kept():
    signals = load_feed({"signals": [
        _sig("SELLR", "insider_trades", "high", 1, headline="Officer sale — 5,000 sh"),
        _sig("BUYR", "insider_trades", "high", 1, headline="Director purchase — 12,000 sh"),
    ]})
    cands = to_candidates(signals, now=NOW)
    assert {c.ticker for c in cands} == {"BUYR"}


def test_non_accumulation_detector_dropped_when_bullish_only():
    signals = load_feed({"signals": [_sig("X", "some_bearish_detector", "high", 1)]})
    assert to_candidates(signals, now=NOW) == []
    # but retained when bullish_only is off
    assert to_candidates(signals, bullish_only=False, now=NOW)


def test_grouping_and_corroboration_ranking():
    signals = load_feed({"signals": [
        _sig("ONE", "volume_anomaly", "high", 1),
        _sig("TWO", "volume_anomaly", "medium", 1),
        _sig("TWO", "insider_trades", "high", 2, headline="cluster buy"),
        _sig("TWO", "dark_pool", "medium", 3),
    ]})
    cands = to_candidates(signals, now=NOW)
    # TWO has 3 distinct detectors -> ranks above ONE (1 detector), despite ONE being high.
    assert cands[0].ticker == "TWO"
    assert cands[0].signal_count == 3
    assert len(cands[0].detectors) == 3
    assert "corroborated" in cands[0].reason


def test_exclude_held_symbols():
    signals = load_feed({"signals": [
        _sig("HELD", "volume_anomaly", "high", 1),
        _sig("FRESH", "volume_anomaly", "high", 1),
    ]})
    cands = to_candidates(signals, exclude={"HELD"}, now=NOW)
    assert {c.ticker for c in cands} == {"FRESH"}


def test_z_suffix_timestamp_parses():
    s = load_feed({"signals": [{
        "ticker": "ZZ", "detector": "volume_anomaly", "severity": "high",
        "occurred_at": "2026-07-27T18:03:00Z",
    }]})
    assert s[0].occurred_at.tzinfo is not None
