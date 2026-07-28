#!/usr/bin/env bash
# Clone (or update) the CAN SLIM analysis skills into ./skills so the agent can load them.
# These skills are the analysis "brain": can-slim-recommend (screener) and
# can-slim-grader (single-ticker grader). They are read-only and never trade.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SKILLS_DIR="$REPO_ROOT/skills"
mkdir -p "$SKILLS_DIR"

# Never hang: on an environment whose network allowlist omits github.com, git can block
# indefinitely instead of failing. Disable credential prompts and cap each operation, so a
# caller (notably the synchronous SessionStart hook) always gets control back.
export GIT_TERMINAL_PROMPT=0
export GIT_ASKPASS=true
GIT_TIMEOUT="${GIT_TIMEOUT:-60}"

clone_or_update() {
  local name="$1" url="$2" dest="$SKILLS_DIR/$1"
  if [ -d "$dest/.git" ]; then
    echo "Updating $name..."
    timeout "$GIT_TIMEOUT" git -C "$dest" pull --ff-only
  else
    echo "Cloning $name..."
    timeout "$GIT_TIMEOUT" git clone --depth 1 "$url" "$dest"
  fi
}

clone_or_update "can-slim-recommend" "https://github.com/thewongdirection/can-slim-recommend.git"
clone_or_update "can-slim-grader"    "https://github.com/thewongdirection/can-slim-grader.git"

echo
echo "Skills installed under $SKILLS_DIR:"
ls -1 "$SKILLS_DIR"
echo "Done. The agent loads SKILL.md from each on startup (see agent/runtime.py)."
