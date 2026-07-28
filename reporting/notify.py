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


def _multipart(fields: dict[str, str], filename: str, payload: bytes,
               file_field: str = "document") -> tuple[bytes, str]:
    """Encode a multipart/form-data body with stdlib only (no requests dependency)."""
    boundary = "----ibkrAgentBoundary7MA4YWxkTrZu0gW"
    crlf = b"\r\n"
    out: list[bytes] = []
    for k, v in fields.items():
        out += [f"--{boundary}".encode(), crlf,
                f'Content-Disposition: form-data; name="{k}"'.encode(), crlf, crlf,
                str(v).encode(), crlf]
    mime = "application/pdf" if filename.lower().endswith(".pdf") else "text/html"
    out += [f"--{boundary}".encode(), crlf,
            f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"'.encode(),
            crlf, f"Content-Type: {mime}".encode(), crlf, crlf, payload, crlf,
            f"--{boundary}--".encode(), crlf]
    return b"".join(out), f"multipart/form-data; boundary={boundary}"


def send_telegram_document(
    path: str,
    *,
    caption: str = "",
    token: str | None = None,
    chat_id: str | None = None,
    timeout: float = 60.0,
) -> bool:
    """Send a file (the dashboard PDF/HTML) to the Telegram chat. Never raises."""
    token = token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return False
    try:
        payload = open(path, "rb").read()
    except OSError as exc:
        print(f"[notify] cannot read {path}: {exc}", file=sys.stderr)
        return False

    name = os.path.basename(path)
    # Telegram caps document captions at 1024 chars (messages at 4096).
    body, content_type = _multipart(
        {"chat_id": chat_id, "caption": (caption or "")[:1024]}, name, payload)
    req = urllib.request.Request(
        f"{TELEGRAM_API}/bot{token}/sendDocument", data=body,
        headers={"Content-Type": content_type})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
        ok = bool(json.loads(raw).get("ok"))
        if not ok:
            print(f"[notify] Telegram rejected the document: {raw[:200]}", file=sys.stderr)
        return ok
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        print(f"[notify] Telegram document delivery failed: {exc}", file=sys.stderr)
        return False


def _find_chromium() -> str | None:
    """Locate a Chromium/Chrome binary for HTML->PDF. Returns None if there isn't one."""
    import glob
    import shutil

    for name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable"):
        found = shutil.which(name)
        if found:
            return found
    # Playwright-managed builds (common in hosted environments).
    for pattern in ("/opt/pw-browsers/chromium*/chrome-linux/chrome",
                    "/opt/pw-browsers/chromium*/chrome-linux/headless_shell",
                    "/root/.cache/ms-playwright/chromium*/chrome-linux/chrome"):
        matches = sorted(glob.glob(pattern))
        if matches:
            return matches[-1]
    return None


def html_to_pdf(html_path: str, pdf_path: str | None = None, timeout: float = 90.0) -> str | None:
    """Render an HTML file to PDF with headless Chromium. Returns the PDF path, or None.

    Best-effort by design: if no browser is available the caller simply sends the HTML
    instead. A missing PDF must never cost you the daily report.
    """
    import subprocess

    browser = _find_chromium()
    if not browser:
        print("[notify] no Chromium found; sending HTML instead of PDF.", file=sys.stderr)
        return None
    pdf_path = pdf_path or os.path.splitext(html_path)[0] + ".pdf"
    try:
        subprocess.run(
            [browser, "--headless", "--disable-gpu", "--no-sandbox", "--no-pdf-header-footer",
             f"--print-to-pdf={pdf_path}", f"file://{os.path.abspath(html_path)}"],
            check=True, capture_output=True, timeout=timeout)
    except (subprocess.SubprocessError, OSError) as exc:
        print(f"[notify] PDF render failed ({exc}); sending HTML instead.", file=sys.stderr)
        return None
    return pdf_path if os.path.exists(pdf_path) else None


def deliver_report(
    brief: str,
    *,
    dashboard_path: str | None = None,
    as_pdf: bool = True,
) -> dict[str, bool]:
    """Deliver the run's output: the summary text, plus the dashboard as an attachment.

    This is the one-way delivery the scheduled bot uses — a readable summary you can act on
    from the lock screen, with the full dashboard attached for when you want the detail.
    The attachment is a PDF when a browser is available, otherwise the raw HTML.
    """
    results = deliver_brief(brief)
    if not dashboard_path or not telegram_configured():
        return results

    send_path = dashboard_path
    if as_pdf:
        send_path = html_to_pdf(dashboard_path) or dashboard_path

    first_line = (brief or "").strip().splitlines()
    caption = first_line[1] if len(first_line) > 1 else (first_line[0] if first_line else "")
    results["telegram_document"] = send_telegram_document(
        send_path, caption=f"Dashboard — {caption}" if caption else "Dashboard")
    return results


def deliver_brief(brief: str) -> dict[str, bool]:
    """Fan the brief out to every configured optional channel. Returns per-channel results.

    Today that's Telegram only; adding e.g. Slack later means one more entry here. Channels
    that aren't configured are simply absent from the returned dict.
    """
    results: dict[str, bool] = {}
    if telegram_configured():
        results["telegram"] = send_telegram(brief)
    return results


def main(argv: list[str] | None = None) -> int:
    """Send a message from the command line: ``python -m reporting.notify "text"``.

    Exists so a scheduled run can deliver a heartbeat or an interim brief in ONE shell command,
    without the caller composing an inline Python snippet (and getting it subtly wrong). Reads
    stdin when no argument is given, so a brief can be piped in. Exit code 0 = delivered.
    """
    import sys as _sys

    args = list(_sys.argv[1:] if argv is None else argv)
    text = " ".join(args).strip() if args else _sys.stdin.read().strip()
    if not text:
        print("usage: python -m reporting.notify \"message\"  (or pipe text on stdin)",
              file=_sys.stderr)
        return 2
    if not telegram_configured():
        print("[notify] Telegram not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID unset).",
              file=_sys.stderr)
        return 1
    ok = send_telegram(text)
    print(f"[notify] telegram: {'sent' if ok else 'FAILED'}", file=_sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
