"""Tests for broker/watchlist.py — parsers, symbol resolution, and the CRUD manager.

The manager is exercised through a fake tool caller that records calls and replays canned
responses shaped per the IBKR watchlist tool schemas (contract_id_ex strings, full-replace
edit semantics).
"""

from __future__ import annotations

import pytest

from broker.watchlist import (
    SymbolNotFound,
    WatchlistManager,
    parse_watchlist,
    parse_watchlists,
)


class FakeCaller:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, tool, args):
        name = tool.split("__")[-1]
        self.calls.append((name, args))
        r = self.responses.get(name, {})
        return r(args) if callable(r) else r

    def names(self):
        return [n for n, _ in self.calls]


def _search(symbol, conid, country="US"):
    return {"contracts": [
        {"symbol": symbol, "underlying_contract_id": conid, "country_code": country,
         "description": f"{symbol} Inc", "sections": [{"sec_type": "STK"}]},
    ]}


# --- parsers ---------------------------------------------------------------

def test_parse_watchlists_defensive_to_shape():
    refs = parse_watchlists({"watchlists": [
        {"id": 12, "name": "Leaders", "hash": "abc"},
        {"id": "13", "name": "Energy"},
        {"name": "no-id-skipped"},
    ]})
    assert [(r.id, r.name) for r in refs] == [("12", "Leaders"), ("13", "Energy")]


def test_parse_watchlist_instruments():
    wl = parse_watchlist({"id": "12", "name": "Leaders", "instruments": [
        {"contract_id_ex": "8314", "contract_description": "IBM"},
        {"contract_id_ex": "4815", "contract_description": "NVDA"},
        {"contract_description": "no-id-skipped"},
    ]})
    assert wl.name == "Leaders"
    assert wl.contract_ids == ["8314", "4815"]


# --- resolution ------------------------------------------------------------

def test_resolve_symbol_picks_exact_us_stock():
    wm = WatchlistManager(FakeCaller({"search_contracts": lambda a: _search("NVDA", 4815747)}))
    r = wm.resolve_symbol("nvda")
    assert r.symbol == "NVDA" and r.contract_id_ex == "4815747"


def test_resolve_symbol_prefers_us_over_foreign_listing():
    rows = {"contracts": [
        {"symbol": "AAPL", "underlying_contract_id": 111, "country_code": "DE",
         "sections": [{"sec_type": "STK"}]},
        {"symbol": "AAPL", "underlying_contract_id": 265598, "country_code": "US",
         "sections": [{"sec_type": "STK"}]},
    ]}
    wm = WatchlistManager(FakeCaller({"search_contracts": rows}))
    assert wm.resolve_symbol("AAPL").contract_id_ex == "265598"


def test_resolve_symbol_not_found():
    wm = WatchlistManager(FakeCaller({"search_contracts": {"contracts": []}}))
    with pytest.raises(SymbolNotFound):
        wm.resolve_symbol("ZZZZ")


def test_resolve_many_splits_missing():
    def search(args):
        q = args["query"].upper()
        return _search(q, 1) if q == "NVDA" else {"contracts": []}

    wm = WatchlistManager(FakeCaller({"search_contracts": search}))
    resolved, missing = wm.resolve_many(["NVDA", "BOGUS"])
    assert [r.symbol for r in resolved] == ["NVDA"] and missing == ["BOGUS"]


# --- create / add / remove / delete ---------------------------------------

def test_add_symbols_creates_when_absent():
    caller = FakeCaller({
        "get_watchlists": {"watchlists": []},
        "search_contracts": lambda a: _search(a["query"].upper(), 900),
        "create_watchlist": lambda a: {"id": "77", "hash": "h"},
    })
    wm = WatchlistManager(caller)
    res = wm.add_symbols("Leaders", ["NVDA", "AVGO"])
    assert res["action"] == "created" and res["id"] == "77"
    assert res["added"] == ["NVDA", "AVGO"]
    # create_watchlist got two stringified contract ids
    _, create_args = next(c for c in caller.calls if c[0] == "create_watchlist")
    assert create_args["name"] == "Leaders"
    assert create_args["instruments"] == ["900", "900"]


def test_add_symbols_merges_into_existing_without_dupes():
    caller = FakeCaller({
        "get_watchlists": {"watchlists": [{"id": "5", "name": "Leaders"}]},
        "get_watchlist": {"id": "5", "name": "Leaders",
                          "instruments": [{"contract_id_ex": "100", "contract_description": "NVDA"}]},
        "search_contracts": lambda a: _search(a["query"].upper(),
                                              100 if a["query"].upper() == "NVDA" else 200),
        "edit_watchlist": {},
    })
    wm = WatchlistManager(caller)
    res = wm.add_symbols("leaders", ["NVDA", "AVGO"])  # NVDA already present
    assert res["action"] == "updated"
    _, edit_args = next(c for c in caller.calls if c[0] == "edit_watchlist")
    assert edit_args["id"] == "5"
    assert edit_args["instruments"] == ["100", "200"]  # merged, deduped, order preserved
    assert res["added"] == ["AVGO"]


def test_remove_symbols_full_replace_minus_dropped():
    caller = FakeCaller({
        "get_watchlists": {"watchlists": [{"id": "5", "name": "Leaders"}]},
        "get_watchlist": {"id": "5", "name": "Leaders", "instruments": [
            {"contract_id_ex": "100"}, {"contract_id_ex": "200"}, {"contract_id_ex": "300"}]},
        "search_contracts": lambda a: _search(a["query"].upper(), 200),
        "edit_watchlist": {},
    })
    wm = WatchlistManager(caller)
    res = wm.remove_symbols("Leaders", ["AVGO"])  # -> conid 200
    _, edit_args = next(c for c in caller.calls if c[0] == "edit_watchlist")
    assert edit_args["instruments"] == ["100", "300"]
    assert res["size"] == 2


def test_remove_symbols_unknown_list_errors():
    wm = WatchlistManager(FakeCaller({"get_watchlists": {"watchlists": []}}))
    with pytest.raises(ValueError):
        wm.remove_symbols("Nope", ["NVDA"])


def test_create_rejects_blank_name():
    wm = WatchlistManager(FakeCaller({}))
    with pytest.raises(ValueError):
        wm.create("   ", ["100"])


def test_delete_calls_connector():
    caller = FakeCaller({"delete_watchlist": {}})
    WatchlistManager(caller).delete("5")
    assert caller.calls == [("delete_watchlist", {"id": "5"})]
