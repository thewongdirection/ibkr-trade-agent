"""Text-command router for the two-way command surface.

Turns a chat line like ``/watch Leaders NVDA AVGO`` into an action + a plain-text reply. It is
deliberately **transport-agnostic**: it knows nothing about Telegram. The Telegram poller in
:mod:`reporting.telegram_bot` (or any other front end, or a test) feeds it strings and sends
back whatever it returns. That keeps the routing logic unit-testable with fake clients.

Capability boundary — commands can **read** the account and **manage watchlists** only. There
is no command that places, modifies, approves, or cancels a trade: order flow stays in the
reviewed, human-approved path by design. ``delete`` is guarded behind an explicit ``CONFIRM``.
"""

from __future__ import annotations

import shlex
from typing import Any

from agent.settings import Settings
from reporting.account_brief import account_brief, positions_brief

HELP = (
    "IBKR bot commands:\n"
    "  /account            — balances, exposure, P&L, top positions\n"
    "  /positions          — full positions list\n"
    "  /watchlists         — your watchlists\n"
    "  /watchlist <name>   — show one list's instruments\n"
    "  /watch <name> <SYM…>   — add symbols (creates the list if new)\n"
    "  /unwatch <name> <SYM…> — remove symbols from a list\n"
    "  /delete <name>      — delete a list (asks you to confirm)\n"
    "  /help               — this message\n"
    "Read-only + watchlists. The bot never trades from a command."
)


class CommandRouter:
    """Route a text command to the account/watchlist capabilities. Never raises to the caller —
    every failure becomes a readable reply string."""

    def __init__(self, settings: Settings, *, broker: Any | None = None,
                 watchlist: Any | None = None):
        self.settings = settings
        self.broker = broker
        self.watchlist = watchlist

    # -- helpers -----------------------------------------------------------

    def _need_broker(self) -> str | None:
        return None if self.broker is not None else (
            "Account data isn't available — the IBKR connector isn't attached to this session.")

    def _need_watchlist(self) -> str | None:
        return None if self.watchlist is not None else (
            "Watchlist actions aren't available — the IBKR connector isn't attached.")

    @staticmethod
    def _fmt_watch_result(r: dict[str, Any]) -> str:
        parts = []
        if r.get("action") == "created":
            parts.append(f"Created watchlist “{r['name']}”.")
        else:
            parts.append(f"Updated “{r['name']}”.")
        if r.get("added"):
            parts.append("Added: " + ", ".join(r["added"]) + ".")
        if r.get("removed"):
            parts.append("Removed: " + ", ".join(r["removed"]) + ".")
        if r.get("unresolved"):
            parts.append("Couldn't resolve: " + ", ".join(r["unresolved"]) + ".")
        if "size" in r:
            parts.append(f"List now holds {r['size']} instrument(s).")
        return " ".join(parts)

    # -- entry point -------------------------------------------------------

    def handle(self, text: str) -> str:
        raw = (text or "").strip()
        if not raw:
            return ""
        if not raw.startswith("/"):
            return "Send /help for the list of commands."
        try:
            tokens = shlex.split(raw)
        except ValueError:
            tokens = raw.split()
        cmd = tokens[0].lstrip("/").lower()
        args = tokens[1:]

        try:
            return self._dispatch(cmd, args)
        except Exception as exc:  # noqa: BLE001 - a command must never crash the bot
            return f"Command failed: {exc}"

    def _dispatch(self, cmd: str, args: list[str]) -> str:
        if cmd in ("help", "start"):
            return HELP

        if cmd in ("account", "summary", "acct"):
            return self._need_broker() or account_brief(self.broker, self.settings)

        if cmd in ("positions", "pos"):
            return self._need_broker() or positions_brief(self.broker, self.settings)

        if cmd in ("watchlists", "lists"):
            if msg := self._need_watchlist():
                return msg
            refs = self.watchlist.list_watchlists()
            if not refs:
                return "You have no watchlists."
            return "Watchlists:\n" + "\n".join(f"  • {r.name}" for r in refs)

        if cmd in ("watchlist", "show"):
            if msg := self._need_watchlist():
                return msg
            if not args:
                return "Usage: /watchlist <name>"
            name = args[0]
            ref = self.watchlist.find_by_name(name)
            if not ref:
                return f"No watchlist named “{name}”."
            wl = self.watchlist.get(ref.id)
            if not wl.instruments:
                return f"“{wl.name}” is empty."
            body = "\n".join(f"  • {i.description or i.contract_id_ex}" for i in wl.instruments)
            return f"“{wl.name}” — {len(wl.instruments)} instrument(s):\n{body}"

        if cmd in ("watch", "add"):
            if msg := self._need_watchlist():
                return msg
            if len(args) < 2:
                return "Usage: /watch <name> <SYMBOL> [SYMBOL …]"
            return self._fmt_watch_result(self.watchlist.add_symbols(args[0], args[1:]))

        if cmd in ("unwatch", "remove"):
            if msg := self._need_watchlist():
                return msg
            if len(args) < 2:
                return "Usage: /unwatch <name> <SYMBOL> [SYMBOL …]"
            return self._fmt_watch_result(self.watchlist.remove_symbols(args[0], args[1:]))

        if cmd in ("delete", "del"):
            if msg := self._need_watchlist():
                return msg
            if not args:
                return "Usage: /delete <name>"
            name = args[0]
            confirmed = len(args) > 1 and args[-1].upper() == "CONFIRM"
            ref = self.watchlist.find_by_name(name)
            if not ref:
                return f"No watchlist named “{name}”."
            if not confirmed:
                return (f"This permanently deletes “{ref.name}”. This can't be undone.\n"
                        f"Reply:  /delete {name} CONFIRM")
            self.watchlist.delete(ref.id)
            return f"Deleted “{ref.name}”."

        return f"Unknown command /{cmd}. Send /help."
