"""Tests for the command router (reporting/commands.py) and account brief."""

from __future__ import annotations

from agent.settings import RiskLimits, Settings
from broker.client import StaticBrokerClient
from broker.watchlist import WatchlistRef, Watchlist, WatchlistInstrument
from reporting.commands import CommandRouter


def make_settings(**overrides) -> Settings:
    risk = RiskLimits(5000, 15, 35, 3, 5, 8, 3, 22)
    base = dict(
        mode="paper", config_mode="paper", base_currency="SGD",
        account_verify={}, strategy_style="blend",
        asset_classes=("stock", "etf", "option"), new_ideas_count=20, risk=risk,
        management={}, universe={}, schedule={},
        recommend_skill_path="skills/can-slim-recommend",
        grader_skill_path="skills/can-slim-grader", journal_db_path="journal/x.db",
    )
    base.update(overrides)
    return Settings(**base)


FUNDED = StaticBrokerClient(
    summary={"currency": "SGD", "net_liquidation": 100000, "total_cash_value": 40000,
             "gross_position_value": 60000},
    positions={"positions": [
        {"symbol": "NVDA", "position": 100, "price": 500, "market_value": 50000,
         "avg_cost": 400, "unrealized_pnl": 10000, "asset_class": "stock"},
        {"symbol": "AVGO", "position": 10, "price": 1000, "market_value": 10000,
         "avg_cost": 1100, "unrealized_pnl": -1000, "asset_class": "stock"},
    ]},
    orders={"orders": []},
)


class FakeWatchlist:
    def __init__(self):
        self.lists = {"5": Watchlist("5", "Leaders", (
            WatchlistInstrument("100", "NVDA Inc"),))}
        self.deleted = []
        self.added = []

    def list_watchlists(self):
        return [WatchlistRef(k, v.name) for k, v in self.lists.items()]

    def find_by_name(self, name):
        for k, v in self.lists.items():
            if v.name.casefold() == name.strip().casefold():
                return WatchlistRef(k, v.name)
        return None

    def get(self, wid):
        return self.lists[wid]

    def add_symbols(self, name, symbols):
        self.added.append((name, tuple(symbols)))
        return {"action": "updated", "name": name, "id": "5",
                "added": [s.upper() for s in symbols], "unresolved": [], "size": 3}

    def remove_symbols(self, name, symbols):
        return {"action": "updated", "name": name, "id": "5",
                "removed": [s.upper() for s in symbols], "unresolved": [], "size": 1}

    def delete(self, wid):
        self.deleted.append(wid)


def router(**kw):
    return CommandRouter(make_settings(), broker=kw.get("broker"),
                         watchlist=kw.get("watchlist"))


def test_help_lists_commands():
    out = router().handle("/help")
    assert "/account" in out and "/watch" in out and "never trades" in out.lower()


def test_non_command_hint():
    assert "help" in router().handle("hello").lower()


def test_account_summary_reports_exposure():
    out = router(broker=FUNDED).handle("/account")
    assert "Equity 100,000 SGD" in out
    assert "NVDA" in out and "+25.0%" in out  # 500 vs 400 cost


def test_positions_lists_all():
    out = router(broker=FUNDED).handle("/positions")
    assert "NVDA" in out and "AVGO" in out


def test_account_without_broker_is_graceful():
    out = router().handle("/account")
    assert "isn't attached" in out or "isn't available" in out


def test_watchlists_listing():
    out = router(watchlist=FakeWatchlist()).handle("/watchlists")
    assert "Leaders" in out


def test_show_watchlist_instruments():
    out = router(watchlist=FakeWatchlist()).handle("/watchlist Leaders")
    assert "NVDA Inc" in out


def test_watch_adds_symbols():
    wl = FakeWatchlist()
    out = router(watchlist=wl).handle("/watch Leaders AVGO MSFT")
    assert wl.added == [("Leaders", ("AVGO", "MSFT"))]
    assert "Added: AVGO, MSFT" in out


def test_watch_requires_symbols():
    out = router(watchlist=FakeWatchlist()).handle("/watch Leaders")
    assert "Usage:" in out


def test_delete_requires_confirmation_then_deletes():
    wl = FakeWatchlist()
    r = router(watchlist=wl)
    first = r.handle("/delete Leaders")
    assert "CONFIRM" in first and wl.deleted == []
    second = r.handle("/delete Leaders CONFIRM")
    assert wl.deleted == ["5"] and "Deleted" in second


def test_delete_unknown_list():
    out = router(watchlist=FakeWatchlist()).handle("/delete Ghost")
    assert "No watchlist" in out


def test_unknown_command():
    assert "/help" in router().handle("/frobnicate")
