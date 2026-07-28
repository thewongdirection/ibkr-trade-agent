"""IBKR watchlist management — the one *write* surface outside order staging.

Reading positions and staging orders already live in :mod:`broker.client` and the review
workflow. This module adds create / edit / delete of **IBKR-side watchlists** so you can curate
lists on command (via the Telegram command bot in :mod:`reporting.telegram_bot`, or the CLI
below). It follows the same transport-agnostic seam as :class:`broker.client.MCPBrokerClient`:
a ``(tool_name, args) -> dict`` caller is injected, so this is usable from the hosted Agent SDK
loop, a standalone SDK MCP session, or a test double, without caring which.

Connector facts this module is built around (from the IBKR MCP tool schemas):

* ``create_watchlist(name, instruments)`` — ``instruments`` are ``contract_id_ex`` strings.
  For a stock that is the **stringified** ``underlying_contract_id`` from ``search_contracts``.
* ``edit_watchlist(id, name, instruments)`` — **full-replace** semantics; to add/remove you
  must read the current list, modify it, and submit the whole thing back.
* ``get_watchlists()`` → every list's ``id`` / ``name`` / ``hash``.
* ``get_watchlist(id)`` → a list's ``name`` + ``instruments`` (each ``contract_id_ex`` +
  ``contract_description``); those ids are round-trippable verbatim.
* ``delete_watchlist(id)`` — irreversible; callers confirm first.

Writes are deliberately kept off the read-only ``BrokerClient`` protocol and out of the CAN
SLIM skills' tool scope — only this manager and the command bot touch them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from broker.client import DEFAULT_TOOL_PREFIX, ToolCaller, _f, _first  # reuse the shared seam


# --------------------------------------------------------------------------- models


@dataclass(frozen=True)
class WatchlistRef:
    """A watchlist as it appears in the index (no instruments loaded)."""

    id: str
    name: str
    hash: str = ""


@dataclass(frozen=True)
class WatchlistInstrument:
    contract_id_ex: str
    description: str = ""


@dataclass(frozen=True)
class Watchlist:
    id: str
    name: str
    instruments: tuple[WatchlistInstrument, ...] = ()

    @property
    def contract_ids(self) -> list[str]:
        return [i.contract_id_ex for i in self.instruments]


@dataclass(frozen=True)
class ResolvedSymbol:
    query: str
    symbol: str
    contract_id_ex: str
    description: str = ""
    country_code: str = ""


class SymbolNotFound(ValueError):
    """Raised when a ticker can't be resolved to a stock contract id."""


# --------------------------------------------------------------------------- parsers


def _rows(data: Any, *keys: str) -> list[dict[str, Any]]:
    """Pull a list of row-dicts from a tool response that may wrap them under any of ``keys``
    (or return a bare list). Stays defensive — connector envelopes vary."""
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        for k in (*keys, "data", "results", "list", "items"):
            v = data.get(k)
            if isinstance(v, list):
                return [r for r in v if isinstance(r, dict)]
    return []


def parse_watchlists(data: dict[str, Any]) -> list[WatchlistRef]:
    out: list[WatchlistRef] = []
    for r in _rows(data, "watchlists"):
        wid = _first(r, "id", "watchlist_id", default="")
        if wid in (None, ""):
            continue
        out.append(WatchlistRef(
            id=str(wid),
            name=str(_first(r, "name", "display_name", default="")),
            hash=str(_first(r, "hash", default="")),
        ))
    return out


def parse_watchlist(data: dict[str, Any], *, watchlist_id: str = "") -> Watchlist:
    instruments: list[WatchlistInstrument] = []
    for r in _rows(data, "instruments"):
        cid = _first(r, "contract_id_ex", "contractIdEx", "contract_id", "conid", default="")
        if cid in (None, ""):
            continue
        instruments.append(WatchlistInstrument(
            contract_id_ex=str(cid),
            description=str(_first(r, "contract_description", "description", "desc", default="")),
        ))
    return Watchlist(
        id=str(_first(data, "id", "watchlist_id", default=watchlist_id)),
        name=str(_first(data, "name", "display_name", default="")),
        instruments=tuple(instruments),
    )


def _pick_stock_row(rows: list[dict[str, Any]], query: str) -> dict[str, Any] | None:
    """Choose the best search_contracts row for a ticker: exact-symbol match, preferring a US
    primary listing, and only rows that actually offer a STK section."""
    q = query.strip().upper()

    def has_stk(r: dict[str, Any]) -> bool:
        secs = r.get("sections") or []
        names = {str(_first(s, "sec_type", "secType", "section", "type", default="")).upper()
                 for s in secs if isinstance(s, dict)}
        # If sections don't spell it out, don't exclude — many rows are plain stocks.
        return not names or "STK" in names

    exact = [r for r in rows if str(_first(r, "symbol", "ticker", default="")).upper() == q]
    candidates = [r for r in exact if has_stk(r)] or exact or rows
    if not candidates:
        return None
    candidates.sort(key=lambda r: 0 if str(r.get("country_code", "")).upper() in ("US", "") else 1)
    return candidates[0]


# --------------------------------------------------------------------------- manager


class WatchlistManager:
    """Create/read/update/delete IBKR watchlists through an injected MCP tool caller."""

    def __init__(self, tool_caller: ToolCaller, tool_prefix: str = DEFAULT_TOOL_PREFIX):
        self._call = tool_caller
        self._prefix = tool_prefix

    def _tool(self, name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        result = self._call(f"{self._prefix}{name}", args or {})
        return result if isinstance(result, dict) else {"data": result}

    # -- reads -------------------------------------------------------------

    def list_watchlists(self) -> list[WatchlistRef]:
        return parse_watchlists(self._tool("get_watchlists"))

    def get(self, watchlist_id: str) -> Watchlist:
        return parse_watchlist(self._tool("get_watchlist", {"id": str(watchlist_id)}),
                               watchlist_id=str(watchlist_id))

    def find_by_name(self, name: str) -> WatchlistRef | None:
        """Case-insensitive name lookup (IBKR keys lists by id, not name)."""
        want = name.strip().casefold()
        for ref in self.list_watchlists():
            if ref.name.strip().casefold() == want:
                return ref
        return None

    def resolve_symbol(self, symbol: str) -> ResolvedSymbol:
        """Resolve a ticker to the ``contract_id_ex`` string a watchlist needs."""
        rows = _rows(self._tool("search_contracts", {"query": symbol}), "contracts")
        row = _pick_stock_row(rows, symbol)
        if not row:
            raise SymbolNotFound(f"no contract found for {symbol!r}")
        conid = _first(row, "underlying_contract_id", "contract_id", "conid", default="")
        if conid in (None, "", 0):
            raise SymbolNotFound(f"{symbol!r} matched but has no contract id")
        return ResolvedSymbol(
            query=symbol,
            symbol=str(_first(row, "symbol", "ticker", default=symbol)).upper(),
            contract_id_ex=str(conid),
            description=str(_first(row, "description", default="")),
            country_code=str(row.get("country_code", "")),
        )

    def resolve_many(self, symbols: list[str]) -> tuple[list[ResolvedSymbol], list[str]]:
        """Resolve a batch; return (resolved, unresolved-symbols)."""
        resolved: list[ResolvedSymbol] = []
        missing: list[str] = []
        for s in symbols:
            try:
                resolved.append(self.resolve_symbol(s))
            except SymbolNotFound:
                missing.append(s.strip().upper())
        return resolved, missing

    # -- writes ------------------------------------------------------------

    def create(self, name: str, contract_ids: list[str]) -> WatchlistRef:
        if not name.strip():
            raise ValueError("watchlist name must be non-blank")
        data = self._tool("create_watchlist",
                          {"name": name.strip(), "instruments": [str(c) for c in contract_ids]})
        new_id = _first(data, "id", "watchlist_id", default="")
        return WatchlistRef(id=str(new_id), name=name.strip(),
                            hash=str(_first(data, "hash", default="")))

    def edit(self, watchlist_id: str, name: str, contract_ids: list[str]) -> None:
        self._tool("edit_watchlist", {"id": str(watchlist_id), "name": name,
                                      "instruments": [str(c) for c in contract_ids]})

    def delete(self, watchlist_id: str) -> None:
        self._tool("delete_watchlist", {"id": str(watchlist_id)})

    # -- convenience add/remove (get -> modify -> full-replace edit) -------

    def add_symbols(self, name: str, symbols: list[str]) -> dict[str, Any]:
        """Add symbols to a named list, creating it if absent. Returns a small result summary."""
        resolved, missing = self.resolve_many(symbols)
        new_ids = [r.contract_id_ex for r in resolved]
        ref = self.find_by_name(name)
        if ref is None:
            created = self.create(name, new_ids)
            return {"action": "created", "name": created.name, "id": created.id,
                    "added": [r.symbol for r in resolved], "unresolved": missing}
        current = self.get(ref.id)
        merged = list(dict.fromkeys([*current.contract_ids, *new_ids]))  # dedupe, keep order
        self.edit(ref.id, current.name or name, merged)
        added = [r.symbol for r in resolved if r.contract_id_ex not in current.contract_ids]
        return {"action": "updated", "name": current.name or name, "id": ref.id,
                "added": added, "unresolved": missing, "size": len(merged)}

    def remove_symbols(self, name: str, symbols: list[str]) -> dict[str, Any]:
        """Remove symbols (by resolving them to ids) from a named list."""
        ref = self.find_by_name(name)
        if ref is None:
            raise ValueError(f"no watchlist named {name!r}")
        current = self.get(ref.id)
        resolved, missing = self.resolve_many(symbols)
        drop = {r.contract_id_ex for r in resolved}
        kept = [c for c in current.contract_ids if c not in drop]
        self.edit(ref.id, current.name, kept)
        return {"action": "updated", "name": current.name, "id": ref.id,
                "removed": [r.symbol for r in resolved], "unresolved": missing, "size": len(kept)}


# --------------------------------------------------------------------------- CLI


def _cli(argv: list[str] | None = None) -> int:
    import argparse

    from agent.settings import load_settings

    parser = argparse.ArgumentParser(
        description="Manage IBKR watchlists (create/list/show/add/remove/delete).")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="List all watchlists.")
    p_show = sub.add_parser("show", help="Show one watchlist by name.")
    p_show.add_argument("name")
    p_add = sub.add_parser("add", help="Add symbols to a list (creates it if new).")
    p_add.add_argument("name"); p_add.add_argument("symbols", nargs="+")
    p_rm = sub.add_parser("remove", help="Remove symbols from a list.")
    p_rm.add_argument("name"); p_rm.add_argument("symbols", nargs="+")
    p_del = sub.add_parser("delete", help="Delete a watchlist by name.")
    p_del.add_argument("name")
    parser.add_argument("--config", default=None)
    args = parser.parse_args(argv)

    settings = load_settings(args.config)
    from agent.runtime import build_watchlist_manager
    try:
        wm = build_watchlist_manager(settings)
        if args.cmd == "list":
            for r in wm.list_watchlists():
                print(f"  [{r.id}] {r.name}")
        elif args.cmd == "show":
            ref = wm.find_by_name(args.name)
            if not ref:
                print(f"No watchlist named {args.name!r}."); return 1
            wl = wm.get(ref.id)
            print(f"{wl.name} [{wl.id}] — {len(wl.instruments)} instruments")
            for i in wl.instruments:
                print(f"  {i.contract_id_ex:<14} {i.description}")
        elif args.cmd == "add":
            print(wm.add_symbols(args.name, args.symbols))
        elif args.cmd == "remove":
            print(wm.remove_symbols(args.name, args.symbols))
        elif args.cmd == "delete":
            ref = wm.find_by_name(args.name)
            if not ref:
                print(f"No watchlist named {args.name!r}."); return 1
            wm.delete(ref.id); print(f"Deleted {ref.name} [{ref.id}].")
    except Exception as exc:  # noqa: BLE001 - surface connector/transport errors cleanly
        import sys
        print(f"Watchlist op failed: {exc}", file=sys.stderr)
        print("\nRun inside a Claude session with the IBKR connector attached, or bind a "
              "transport in build_watchlist_manager() (see TODO(connector)).", file=sys.stderr)
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    return _cli(argv)


if __name__ == "__main__":
    raise SystemExit(main())
