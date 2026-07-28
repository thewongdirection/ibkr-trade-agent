"""Two-way Telegram command bot — the inbound half of :mod:`reporting.notify`.

``notify`` sends the daily brief *out*. This module lets you send commands *in*: message the
bot ``/account`` or ``/watch Leaders NVDA`` and it replies. It polls Telegram's ``getUpdates``
(long-poll, stdlib only, honours ``HTTPS_PROXY``), routes each command through
:class:`reporting.commands.CommandRouter`, and replies with :func:`reporting.notify.send_telegram`.

Security — **only the configured chat may command the bot.** Every update whose ``chat.id``
does not equal ``TELEGRAM_CHAT_ID`` is ignored (and logged), so a stranger who finds the bot
cannot read your account or touch your watchlists. Commands are read + watchlist only; there is
no trade-execution command.

Running it — the connector transport must be bound for live data (same ``TODO(connector)`` as
the rest of the repo: run inside a Claude session with the IBKR connector, or bind an SDK MCP
client). Two modes:
  * ``--once`` drains any pending commands and exits — drive it from a scheduler.
  * ``--loop`` long-polls continuously — for a self-hosted process.
The last processed update id is persisted so ``--once`` never re-answers an old message.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

from agent.settings import Settings, load_settings
from reporting.commands import CommandRouter
from reporting.notify import TELEGRAM_API, send_telegram


def _offset_path(settings: Settings) -> Path:
    """State file for the last-seen update id, kept beside the journal db."""
    return Path(settings.journal_db_path).expanduser().resolve().parent / "telegram_offset.txt"


def load_offset(settings: Settings) -> int:
    try:
        return int(_offset_path(settings).read_text().strip() or "0")
    except (OSError, ValueError):
        return 0


def save_offset(settings: Settings, offset: int) -> None:
    try:
        p = _offset_path(settings)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(str(offset))
    except OSError as exc:  # pragma: no cover - best effort
        print(f"[bot] could not persist offset: {exc}", file=sys.stderr)


def get_updates(token: str, *, offset: int = 0, timeout: int = 0) -> list[dict[str, Any]]:
    """Call Telegram ``getUpdates``. Returns the raw update dicts (empty on any error)."""
    params = urllib.parse.urlencode(
        {"offset": offset, "timeout": timeout, "allowed_updates": json.dumps(["message"])})
    url = f"{TELEGRAM_API}/bot{token}/getUpdates?{params}"
    try:
        with urllib.request.urlopen(url, timeout=timeout + 15) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        print(f"[bot] getUpdates failed: {exc}", file=sys.stderr)
        return []
    if not data.get("ok"):
        print(f"[bot] getUpdates rejected: {str(data)[:200]}", file=sys.stderr)
        return []
    return data.get("result", []) or []


def _extract(update: dict[str, Any]) -> tuple[int, str, str]:
    """(update_id, chat_id, text) from an update; chat_id/text empty when not a text message."""
    update_id = int(update.get("update_id", 0))
    msg = update.get("message") or update.get("edited_message") or {}
    chat_id = str((msg.get("chat") or {}).get("id", ""))
    text = str(msg.get("text", "") or "")
    return update_id, chat_id, text


def poll_once(
    router: CommandRouter,
    *,
    token: str,
    authorized_chat_id: str,
    offset: int = 0,
    timeout: int = 0,
    reply: Callable[..., bool] = send_telegram,
    fetch: Callable[..., list[dict[str, Any]]] = get_updates,
) -> tuple[int, int]:
    """Fetch and handle one batch of updates.

    Returns ``(next_offset, handled_count)``. Messages from any chat other than
    ``authorized_chat_id`` are ignored. ``reply``/``fetch`` are injectable for tests.
    """
    updates = fetch(token, offset=offset, timeout=timeout)
    next_offset = offset
    handled = 0
    for up in updates:
        update_id, chat_id, text = _extract(up)
        next_offset = max(next_offset, update_id + 1)
        if not text:
            continue
        if str(chat_id) != str(authorized_chat_id):
            print(f"[bot] ignoring command from unauthorized chat {chat_id}", file=sys.stderr)
            continue
        answer = router.handle(text)
        if answer:
            reply(answer, token=token, chat_id=chat_id)
            handled += 1
    return next_offset, handled


def build_router(settings: Settings) -> CommandRouter:
    """Wire a router to the live connectors (unbound transport raises on first use, as elsewhere)."""
    broker = watchlist = None
    try:
        from agent.runtime import build_broker_client, build_watchlist_manager
        broker = build_broker_client(settings)
        watchlist = build_watchlist_manager(settings)
    except Exception as exc:  # noqa: BLE001 - degrade to help-only if unbound
        print(f"[bot] connector not bound ({exc}); commands will report unavailability.",
              file=sys.stderr)
    return CommandRouter(settings, broker=broker, watchlist=watchlist)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Two-way IBKR Telegram command bot.")
    g = parser.add_mutually_exclusive_group()
    g.add_argument("--once", action="store_true",
                   help="Drain pending commands once and exit (for a scheduler).")
    g.add_argument("--loop", action="store_true", help="Long-poll continuously (self-hosted).")
    parser.add_argument("--timeout", type=int, default=25,
                        help="Long-poll timeout seconds (loop mode).")
    parser.add_argument("--config", default=None)
    args = parser.parse_args(argv)

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        print("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must both be set.", file=sys.stderr)
        return 2

    settings = load_settings(args.config)
    router = build_router(settings)
    offset = load_offset(settings)

    if args.loop:
        print("[bot] long-polling for commands (Ctrl-C to stop)…", file=sys.stderr)
        try:
            while True:
                offset, handled = poll_once(
                    router, token=token, authorized_chat_id=chat_id,
                    offset=offset, timeout=args.timeout)
                if handled:
                    save_offset(settings, offset)
                time.sleep(1)
        except KeyboardInterrupt:
            save_offset(settings, offset)
            print("\n[bot] stopped.", file=sys.stderr)
        return 0

    # default: one-shot drain
    offset, handled = poll_once(router, token=token, authorized_chat_id=chat_id,
                                offset=offset, timeout=0)
    save_offset(settings, offset)
    print(f"[bot] handled {handled} command(s).", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
