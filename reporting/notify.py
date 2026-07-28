"""Out-of-band delivery of the review brief — currently Telegram.

The daily Routine already posts the brief into your Claude chat and fires a completion push.
This module adds an **optional** extra channel so the same brief lands in a Telegram chat you
control (handy for a phone alert independent of the Claude app).

Design rules:
- **Opt-in via environment, no secrets in the repo.** Sending is enabled only when both
  ``TELEGRAM_BOT_TOKEN`` and ``TELEGRAM_CHAT_ID`` are set. Neither lives in git — put them in
  your git-ignored ``.env`` (local) or the Routine environment (scheduled). See docs/RUNNING.md.
- **Never break the review.** A delivery failure (network, bad token, Telegram down) must not
  fail the run — every send returns a bool and swallows its own errors with a warning.
- **Stdlib only.** Uses ``urllib`` (which honours the ``HTTPS_PROXY`` env), so no new deps.

Set up a bot in ~2 min: message @BotFather → ``/newbot`` → copy the token; then message your
new bot once and read the chat id from ``https://api.telegram.org/bot<token>/getUpdates``.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

TELEGRAM_API = "https://api.telegram.org"
_TELEGRAM_MAX_CHARS = 4096  # Telegram hard limit for a single message.


def telegram_configured() -> bool:
    """True when both the bot token and chat id are present in the environment."""
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"))


def send_telegram(
    text: str,
    *,
    token: str | None = None,
    chat_id: str | None = None,
    timeout: float = 10.0,
) -> bool:
    """Send ``text`` to a Telegram chat. Returns True on success, False otherwise.

    Falls back to ``TELEGRAM_BOT_TOKEN`` / ``TELEGRAM_CHAT_ID`` from the environment when the
    args are omitted. Never raises — a delivery problem prints a warning and returns False so
    the caller (the review) keeps going.
    """
    token = token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return False

    body = (text or "").strip()
    if not body:
        return False
    if len(body) > _TELEGRAM_MAX_CHARS:
        body = body[: _TELEGRAM_MAX_CHARS - 1] + "…"  # ellipsis

    payload = urllib.parse.urlencode(
        {"chat_id": chat_id, "text": body, "disable_web_page_preview": "true"}
    ).encode("utf-8")
    url = f"{TELEGRAM_API}/bot{token}/sendMessage"
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
        ok = bool(json.loads(raw).get("ok"))
        if not ok:
            print(f"[notify] Telegram rejected the message: {raw[:200]}", file=sys.stderr)
        return ok
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        print(f"[notify] Telegram delivery failed: {exc}", file=sys.stderr)
        return False


def deliver_brief(brief: str) -> dict[str, bool]:
    """Fan the brief out to every configured optional channel. Returns per-channel results.

    Today that's Telegram only; adding e.g. Slack later means one more entry here. Channels
    that aren't configured are simply absent from the returned dict.
    """
    results: dict[str, bool] = {}
    if telegram_configured():
        results["telegram"] = send_telegram(brief)
    return results
