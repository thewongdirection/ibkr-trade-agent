"""Paper/live mode switching.

The effective mode is gated by a two-switch interlock (see agent/settings.py):
``account.mode: live`` in config.yaml AND ``IBKR_ALLOW_LIVE=1`` in the environment. This
module flips the *config* switch (a targeted, comment-preserving edit) and reports what still
needs to happen for a change to take effect. It never touches the environment switch — that
stays a deliberate, out-of-band action so code can't silently arm live trading.

CLI:
    python -m broker.mode status     # show config vs effective mode + what to do
    python -m broker.mode paper      # set config back to paper (always safe)
    python -m broker.mode live       # set config to live (still needs IBKR_ALLOW_LIVE=1)
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from agent.settings import DEFAULT_CONFIG_PATH, load_settings

_MODE_LINE = re.compile(r"^(\s*mode:\s*)(paper|live)(\s*(?:#.*)?)$", re.MULTILINE)


def set_config_mode(target: str, config_path: str | os.PathLike[str] | None = None) -> str:
    """Set ``account.mode`` in config.yaml to ``target``, preserving comments/formatting.

    Returns the previous mode. Raises on an unexpected/unknown config shape rather than
    risk writing a malformed file.
    """
    target = target.strip().lower()
    if target not in ("paper", "live"):
        raise ValueError(f"mode must be 'paper' or 'live', got {target!r}")

    path = Path(config_path or DEFAULT_CONFIG_PATH)
    text = path.read_text()

    match = _MODE_LINE.search(text)
    if not match:
        raise ValueError(
            f"could not find an 'account.mode' line to update in {path}; edit it by hand."
        )
    previous = match.group(2)
    new_text = _MODE_LINE.sub(rf"\g<1>{target}\g<3>", text, count=1)
    path.write_text(new_text)
    return previous


def env_allows_live() -> bool:
    return os.environ.get("IBKR_ALLOW_LIVE", "").strip() in ("1", "true", "yes")


def status_report(config_path: str | os.PathLike[str] | None = None) -> str:
    settings = load_settings(config_path)
    lines = [
        f"Config account.mode : {settings.config_mode}",
        f"IBKR_ALLOW_LIVE env : {'set' if env_allows_live() else 'not set'}",
        f"EFFECTIVE mode      : {settings.mode.upper()}",
    ]
    if settings.config_mode == "live" and not env_allows_live():
        lines.append("")
        lines.append("Config requests LIVE but the env interlock is not armed -> still PAPER.")
        lines.append("To actually trade live: export IBKR_ALLOW_LIVE=1 (then re-run).")
    elif settings.mode == "live":
        lines.append("")
        lines.append("*** LIVE TRADING IS ARMED *** real money. Orders still require your")
        lines.append("one-click approval in IBKR; the agent never auto-executes.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    cmd = (argv[0] if argv else "status").strip().lower()

    if cmd == "status":
        print(status_report())
        return 0

    if cmd in ("paper", "live"):
        previous = set_config_mode(cmd)
        print(f"config account.mode: {previous} -> {cmd}")
        if cmd == "live":
            print()
            print("Config now requests LIVE. This alone does NOT arm live trading.")
            print("You must also set the environment interlock in the shell that runs the agent:")
            print("    export IBKR_ALLOW_LIVE=1")
            print("Leave it unset to stay on paper. Verify with: python -m broker.mode status")
        else:
            print("Back to paper trading (the safe default).")
        return 0

    print(f"unknown command {cmd!r}; use: status | paper | live", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
