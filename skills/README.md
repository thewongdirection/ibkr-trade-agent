# skills/

The CAN SLIM analysis skills live here at runtime. They are **not** vendored into this repo
(they have their own repos and their own release cadence) — instead they are cloned in by
`scripts/setup_skills.sh` and are git-ignored.

| Skill | Repo | Role |
|-------|------|------|
| `can-slim-recommend` | https://github.com/thewongdirection/can-slim-recommend | Market-wide screener → ranked shortlist of *new* ideas |
| `can-slim-grader` | https://github.com/thewongdirection/can-slim-grader | Single-ticker → C·A·N·S·L·I·M scorecard + BUY-RANGE/WATCH/AVOID verdict |

## Install

```bash
scripts/setup_skills.sh
```

This clones both into `skills/can-slim-recommend` and `skills/can-slim-grader`. The agent
loads each `SKILL.md` on startup (paths come from `config.yaml → skills`).

## Why they're separate from the trade agent

Both skills are strictly **read-only market analysis** — by design they never call order or
account tools. The trade agent grants them only the read-only IBKR tool set
(`analysis/canslim.py → CANSLIM_READONLY_TOOLS`). All trading, sizing, and risk enforcement
happens in the trade agent, never inside a skill.

## Alternative: git submodules

If you prefer pinned versions committed with the repo, replace the clone script with
submodules:

```bash
git submodule add https://github.com/thewongdirection/can-slim-recommend skills/can-slim-recommend
git submodule add https://github.com/thewongdirection/can-slim-grader    skills/can-slim-grader
```

and drop the two `skills/can-slim-*` entries from `.gitignore`.
