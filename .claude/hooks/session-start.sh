#!/bin/bash
# SessionStart hook — make this repo runnable the moment a session opens.
#
# Two things every session needs and neither is in git:
#   1. the package installed (so `python -m workflows.daily_review`, the CLIs and pytest work)
#   2. the CAN SLIM analysis skills cloned into ./skills (git-ignored; they ARE the
#      grading/screening brain — without them the daily review degrades to an informational
#      brief and stages nothing)
#
# This matters most for the scheduled Routine, which fires a FRESH session each morning: if
# the skills aren't installed there, the review silently loses its analysis. Installing here
# means a run never depends on a prompt remembering to do it.
#
# Idempotent: pip is a no-op when satisfied, and setup_skills.sh pulls instead of re-cloning.
set -euo pipefail

cd "${CLAUDE_PROJECT_DIR:-$(dirname "$0")/../..}"

echo "[session-start] installing package…"
# A full editable install can fail on a system-managed transitive dep (e.g. a distro-installed
# PyJWT with no RECORD). Fall back to --no-deps so the package itself still installs, then
# guarantee imports work regardless by putting the repo root on PYTHONPATH for the session.
if ! pip install -e . -q >/dev/null 2>&1; then
  echo "[session-start] full install failed (likely a system-managed dep); retrying --no-deps…"
  pip install -e . -q --no-deps >/dev/null 2>&1 \
    || echo "[session-start] WARNING: editable install failed; relying on PYTHONPATH"
fi
if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  echo "export PYTHONPATH=\"${CLAUDE_PROJECT_DIR:-$PWD}:\${PYTHONPATH:-}\"" >> "$CLAUDE_ENV_FILE"
fi

# The skills live in separate repos. A clone failure must NOT abort the session — the review
# degrades gracefully — so warn loudly and continue.
echo "[session-start] installing CAN SLIM skills…"
if bash scripts/setup_skills.sh >/dev/null 2>&1; then
  echo "[session-start] skills OK: $(ls -1 skills | grep -c can-slim || true) installed"
else
  echo "[session-start] WARNING: could not install CAN SLIM skills."
  echo "[session-start]   The daily review will run in INFORMATIONAL mode (no grading/screening)."
  echo "[session-start]   Fix: check access to the can-slim-recommend / can-slim-grader repos."
fi

echo "[session-start] ready."
