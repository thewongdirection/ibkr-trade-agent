# Hosting the trading bot

How to run the daily review on a schedule. Three paths, from zero-infra to full self-host.
Your configuration — **stage every order for one-tap approval** — means the bot never needs
to place fills unattended, so **Path A needs no server and no IB Gateway**. Start there.

---

## TL;DR — which path?

| | Path A: Claude Routine | Path B: Self-hosted VM | Path C: GitHub Actions |
|---|---|---|---|
| Infra to run | **None** | A always-on Linux VM | None (ephemeral runners) |
| IB Gateway needed | No | Yes (for auto-execute) | No (connector) / N/A |
| Can auto-execute fills | No (stage + approve) | **Yes** | No |
| Ops burden | Minimal | High (gateway babysitting) | Low |
| Best for | **Your setup today** | Later, if you want hands-off fills | Lightweight cron, stage-only |
| Cost | Your Claude plan | ~$5–12/mo VM | Free tier |

**Recommendation: Path A now.** Move to Path B only if you later want the bot to fill certain
trades without you tapping approve (that's the "auto lane", and it's the only reason to take
on IB Gateway).

---

## Path A — Claude Routine (recommended, zero infra)

A **Routine** is a scheduled trigger that fires a fresh Claude session on a cron schedule.
Each firing clones this repo, has your connectors (IBKR, FMP) and the CAN SLIM skills
available, runs `workflows/daily_review.py`, and delivers the dashboard + push + chat brief.

### How it works
```
  cron (pre-market)  ─►  fresh Claude session  ─►  daily_review
                          │  IBKR connector: pull account/positions
                          │  CAN SLIM skills: grade holdings + recommend
                          │  risk layer: size + cap-check
                          │  IBKR connector: STAGE orders (you approve)
                          └─ deliver: HTML dashboard + push + chat brief
```

### Setup
1. **Authorize the connectors** you need in claude.ai → Settings → Connectors: **Interactive
   Brokers** and **Financial Modeling Prep**. (Gmail too if you later want email.)
2. **Install the skills** in the environment: `scripts/setup_skills.sh` (or commit them as
   submodules) so `can-slim-recommend` / `can-slim-grader` load on each run.
3. **Create the Routine.** Either ask me ("set up the daily Routine") and I'll create it, or
   do it yourself with a scheduled trigger pointing at the prompt in
   `workflows/ROUTINE_PROMPT.md`. Schedule (cron is **UTC**):
   - US pre-market 08:30 **ET** = **12:30 UTC** during EDT (Mar–Nov), 13:30 UTC during EST.
   - Weekdays only: `30 12 * * 1-5` (EDT) — I'll set the right one for the season.
4. **First runs on paper.** Watch a few daily runs end-to-end before trusting it.

### The one caveat to know
Scheduled/headless sessions can only use connectors that are **authorized for the
environment**, not ones that need an interactive OAuth tap at run time. If a firing reports
the IBKR connector as unavailable, re-authorize it and confirm it persists for scheduled
runs. (This is exactly the reconnect dance we already hit once — for a daily bot it needs to
stay authorized.)

### Pros / cons
- **Pros:** no server, no IB Gateway, no stored broker credentials, auto-updates with the repo.
- **Cons:** depends on connector availability in scheduled sessions; cannot place unattended
  fills (by design — you approve). Perfectly matched to your "stage all, I approve" choice.

---

## Path B — Self-hosted VM (for future hands-off execution)

Only needed if you later want the bot to **execute certain trades unattended**. This is where
`ib_async` + **IB Gateway** come in, and it is a real operational commitment.

### Components
```
  systemd timer (daily)
        │
        ▼
  Python app (Claude Agent SDK) ──► Anthropic API
        │
        ▼
  IB Gateway (headless, kept alive by IBC) ──► IBKR
```

### Step-by-step
1. **Provision a VM.** 1–2 GB RAM Linux box (Hetzner ~€4/mo, DigitalOcean/Lightsail ~$6/mo,
   or Fly.io). Keep it in a region with low latency to IBKR if latency matters (it won't for a
   daily bot).
2. **Install the app.**
   ```bash
   git clone <this repo> && cd ibkr-trade-agent
   python -m venv .venv && source .venv/bin/activate
   pip install -e .
   scripts/setup_skills.sh
   ```
3. **Run IB Gateway headless with IBC.** IB Gateway is a GUI app; **IBC**
   (github.com/IbcAlpha/IBC) automates its login and keeps it running.
   - Install IB Gateway + IBC; run under `xvfb` (virtual display) in Docker. Good prebuilt
     images exist (e.g. `gnzsnz/ib-gateway`).
   - IBKR forces a **daily re-authentication** (~midnight account-time) — configure IBC's
     auto-restart so the gateway logs back in each day.
   - **2FA is the hard part for headless.** IBKR's IBKR-Mobile 2FA blocks silent logins.
     Options: use the **paper account** (often lighter 2FA), or IBKR's "Secure Login System"
     exemptions for a dedicated automation user. Never disable 2FA on a funded live account
     casually.
4. **Store secrets safely — never in git.**
   - `ANTHROPIC_API_KEY` and IBKR credentials for IBC go in the VM's environment or a secrets
     manager (systemd `EnvironmentFile=` with `chmod 600`, Docker secrets, or Vault).
   - `.env` stays git-ignored (already enforced). `IBKR_ALLOW_LIVE` only on the box you intend
     to trade live from.
5. **Point the app at the gateway.** Bind the broker client to `ib_async` on
   `127.0.0.1:4002` (paper) / `4001` (live) instead of the hosted MCP connector — this is the
   `TODO(connector)` seam in `agent/runtime.py::build_broker_client`.
6. **Schedule it.** A systemd timer beats cron for logging/retries:
   ```ini
   # /etc/systemd/system/ibkr-review.timer
   [Timer]
   OnCalendar=Mon..Fri 12:30 UTC
   Persistent=true
   [Install]
   WantedBy=timers.target
   ```
   Pair with a `ibkr-review.service` that runs `python -m workflows.daily_review --stage`.
7. **Deliver updates.** No Claude chat here — use a push service (Pushover, ntfy, or a
   Telegram bot) for alerts, and write the dashboard to a small static host (or email it).
8. **Monitor.** Alert on: gateway down, daily run failed, order rejected. A dead gateway that
   silently skips a day is the classic failure.

### Pros / cons
- **Pros:** full control; can auto-execute within the risk caps; independent of Claude session
  availability.
- **Cons:** you now babysit IB Gateway (daily re-auth, 2FA, restarts); you store broker
  credentials; more attack surface; more to monitor.

---

## Path C — GitHub Actions (lightweight cron, stage-only)

A scheduled Actions workflow runs the Python review on a free runner.

```yaml
# .github/workflows/daily-review.yml
on:
  schedule:
    - cron: "30 12 * * 1-5"   # 08:30 ET (EDT), UTC
  workflow_dispatch: {}
jobs:
  review:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.11" }
      - run: pip install -e .
      - run: python -m workflows.daily_review        # dry run; add --stage when wired
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
```

- **Secrets** go in the repo's **Actions Secrets** (Settings → Secrets), never in the YAML.
- **Caveat:** Actions runners are **ephemeral**, so they cannot keep IB Gateway alive — this
  path only works for the **hosted connector / stage-only** model or an IBKR **Web API**
  gateway hosted elsewhere. Fine for stage-and-notify; not for unattended fills.
- Delivery: push/Telegram or commit the dashboard to the repo / Pages.

---

## Security checklist (all paths)
- No credentials in git — only `.env.example` placeholders; `.env` is git-ignored.
- Live trading requires **both** `account.mode: live` and `IBKR_ALLOW_LIVE=1` (two switches).
- Every order is staged for approval on your setup — the bot has no execute path.
- Rotate `ANTHROPIC_API_KEY` if it ever leaks; scope broker automation to a dedicated user.
- Keep the identity check armed (`account.verify`) so a run against the wrong account aborts.
