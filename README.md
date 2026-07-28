# ibkr-trade-agent

An assistant that manages an **Interactive Brokers (IBKR)** account: every weekday pre-market
it reviews your portfolio, hunts for new positions and manages existing ones using the
**CAN SLIM** growth-investing methodology, and stages trades for your **one-click
approval**. It never executes a trade on its own.

> **Not investment advice.** This software is decision-support tooling. It pairs every
> idea with a loss-cutting exit rule, but markets carry risk of loss and you are
> responsible for every order you approve. Start on a **paper account**.

## ▶ Start here

**New to this repo? Read [`docs/RUNNING.md`](docs/RUNNING.md)** — the explicit, step-by-step
setup & run guide (which connectors to attach, where, and how to run it).

**Attach these two Claude connectors** (the agent has no broker credentials of its own):

| Connector | For | Required |
|---|---|---|
| **Interactive Brokers (IBKR)** | Read the account & stage orders for approval | **Yes** |
| **Financial Modeling Prep (FMP)** | Fundamentals + quotes feeding CAN SLIM | **Yes** |

Attach them in **two places**: your **claude.ai account** (Settings → Connectors) *and*, for
the scheduled bot, the **Routine itself** (Routines → open it → attach connectors). A headless
Routine run does **not** inherit your interactive connectors — this is the #1 cause of a
"connector unavailable" scheduled run. Details in [`docs/RUNNING.md`](docs/RUNNING.md).

### What each daily run does

1. **Verify** the IBKR connection + that it's *your* account (aborts on mismatch); report mode.
2. **Read** balances + positions (informational only if unfunded).
3. **Grade holdings** — market direction first, then `can-slim-grader` → hold / trim / exit
   (honors the 7–8% stop; flags options near expiry).
4. **Hunt ideas** — `can-slim-recommend` screen + the monitor signal feed; only BUY-RANGE names
   with a valid pivot survive.
5. **Size + risk-check** every buy against the `config.yaml` caps; attach a stop to each entry.
6. **Stage** survivors for one-tap approval (never executes); **journal** every decision.
7. **Deliver** — HTML dashboard + concise chat brief + completion push, and (optional) the same
   brief to a **Telegram chat** you control — see [docs/RUNNING.md §8](docs/RUNNING.md#8-also-get-the-brief-on-telegram-optional).

> **Setting this up?** [docs/RUNNING.md](docs/RUNNING.md) is the authoritative, step-by-step
> guide: the two connectors and **where** to attach them, the Routine/schedule, paper-vs-live,
> and the optional Telegram delivery (including the easy-to-miss `api.telegram.org` allowlist).

---

## Architecture

Two halves with a deliberate separation of concerns:

```
                       ┌─────────────────────────────────────────────┐
                       │                THE BRAIN                      │
                       │  CAN SLIM skills (read-only, never trade)     │
                       │                                               │
   new ideas  ◄────────┤  can-slim-recommend  → ranked shortlist       │
   grade a name ◄──────┤  can-slim-grader     → C·A·N·S·L·I·M verdict  │
                       └───────────────────────┬───────────────────────┘
                                               │ recommendations / grades
                                               ▼
                       ┌─────────────────────────────────────────────┐
                       │              THE HANDS + RISK                 │
                       │  ibkr-trade-agent (this repo)                 │
                       │                                               │
                       │  analysis/positions  portfolio analytics      │
                       │  risk/guardrails     caps + paper/live gate    │
                       │  journal/store       trade + rationale log     │
                       │  workflows/daily     the scheduled review      │
                       │                                               │
                       │  → stages orders via create_order_instruction  │
                       │    for your ONE-CLICK APPROVAL (never auto)    │
                       └─────────────────────────────────────────────┘
```

- **The brain** — the [`can-slim-recommend`](https://github.com/thewongdirection/can-slim-recommend)
  and [`can-slim-grader`](https://github.com/thewongdirection/can-slim-grader) Claude Skills.
  They are strictly read-only: they screen the market and grade tickers against CAN SLIM but
  **never** call order or account tools. `recommend` finds *new* ideas; `grader` judges a
  *named* holding (used each day to decide hold / trim / exit).
- **The hands + risk** — this repo. It reads your positions and balances, runs the daily
  review, applies the CAN SLIM output through a **risk layer** (position and per-order caps,
  a hard paper/live switch), stages orders for approval, and journals every decision with its
  rationale so each day's run has memory of *why* you hold what you hold.

### Technology stack

| Concern            | Choice                                   | Why |
|--------------------|------------------------------------------|-----|
| Language           | Python 3.11+                             | Best financial ecosystem |
| Agent runtime      | [Claude Agent SDK](https://docs.claude.com/en/api/agent-sdk) | Native MCP; the IBKR + FMP connectors plug straight in |
| Broker connection  | IBKR MCP connector                       | No infra to run; order-instruction flow gives the approval gate for free |
| Market data        | FMP + IBKR (+ optional Massive)          | Fundamentals + technicals for the CAN SLIM "blend" |
| Analysis           | `can-slim-recommend` / `can-slim-grader` | The scoring brain |
| Scheduling         | Routine / cron → `workflows/daily_review.py` | The weekday pre-market cadence |
| State / memory     | SQLite (`journal/`)                      | Trade log + rationale, committed with the repo |

---

## Safety model (read this)

1. **Paper first.** `config.yaml` ships with `account.mode: paper`. Flipping to `live`
   requires editing the file *and* setting `IBKR_ALLOW_LIVE=1` in the environment — two
   independent switches so it can't happen by accident.
2. **Propose, never auto-execute.** The agent only ever *stages* orders with
   `create_order_instruction`. You approve or reject each one in IBKR. There is no code path
   that confirms an order.
3. **Hard caps.** `risk/guardrails.py` rejects any staged order that breaches the per-order
   notional cap, per-position weight cap, or sector-concentration cap in `config.yaml` —
   before it is ever staged.
4. **Everything journaled.** Every recommendation, grade, staged order, and rejection is
   written to the SQLite journal with a timestamp and rationale.

---

## Setup

```bash
# 1. Install
python -m venv .venv && source .venv/bin/activate
pip install -e .

# 2. Pull the CAN SLIM analysis skills into ./skills
scripts/setup_skills.sh

# 3. Configure
cp .env.example .env          # add ANTHROPIC_API_KEY; keep IBKR_ALLOW_LIVE unset
$EDITOR config.yaml           # review caps and universe; leave account.mode: paper

# 4. Verify guardrails
pytest -q
```

The IBKR and FMP connectors are provided as MCP servers by your Claude environment; the
agent discovers them at runtime (see `agent/runtime.py`). No API keys for those live in
this repo.

## Running

**Full instructions: [`docs/RUNNING.md`](docs/RUNNING.md).** The essentials:

```bash
# Verify the IBKR connection + that it's YOUR account (fingerprint + identity check):
python -m broker.session

# Retrieve account info, positions, open orders, and trade history:
python -m broker.account --all
python -m broker.account --trades YEAR_TO_DATE

# Check / switch paper<->live mode (see "Safety model" above):
python -m broker.mode status
python -m broker.mode live      # + export IBKR_ALLOW_LIVE=1 to actually arm live

# One daily review on demand (stages nothing without --stage):
python -m workflows.daily_review
python -m workflows.daily_review --out dashboard.html   # also write the HTML dashboard

# Stage the approved-shape orders for one-click approval in IBKR:
python -m workflows.daily_review --stage
```

To run it in a chat, ask the assistant to **"run the daily IBKR review"** (with the IBKR + FMP
connectors attached). To run it automatically each weekday, use a **Routine** — see
[`docs/RUNNING.md`](docs/RUNNING.md) §5C and [`docs/HOSTING.md`](docs/HOSTING.md) → Path A.

See [`docs/CONNECTING.md`](docs/CONNECTING.md) for how the IBKR connection works, why no
credentials live in this repo, and how to verify the connected account is yours.

### Account login & identity

There is no password to store: the IBKR MCP connector is authorized via Claude's connector
OAuth, so "login" here means verifying the connector is reachable and identifying the
account. The connector **masks the account number and owner name**, so `python -m broker.session`
establishes identity from a fingerprint you recognize (base currency, account inception date,
positions, recent trades) and an optional marker you set in `config.yaml → account.verify`.
With a marker set, the agent refuses to operate if the live account doesn't match.

To run it automatically each weekday, point a Routine / cron entry at
[`workflows/ROUTINE_PROMPT.md`](workflows/ROUTINE_PROMPT.md) — see
[`docs/RUNNING.md`](docs/RUNNING.md) §5C and [`docs/HOSTING.md`](docs/HOSTING.md) → Path A.

## Layout

```
agent/       Agent SDK runtime, settings loader (paper/live interlock), system prompt
broker/      IBKR connection, account retrieval, identity verify, mode switching
  client.py    typed AccountSummary/Balance/Position/Trade/Order + parsers
  session.py   connection + account-identity verification ("login")
  account.py   CLI: account info, positions, orders, trade history
  mode.py      CLI: paper<->live switch (honors the two-switch interlock)
analysis/    portfolio analytics + CAN SLIM skill wiring
risk/        guardrails: caps, paper/live gate, approval-shape checks
signals/     stock-movement-monitor signal-feed consumer (candidate source)
journal/     SQLite trade + rationale log
reporting/   dashboard.py — self-contained HTML dashboard
workflows/   daily_review.py (scheduled entry point) + ROUTINE_PROMPT.md
skills/      CAN SLIM skills (populated by scripts/setup_skills.sh; git-ignored)
scripts/     setup_skills.sh
docs/        RUNNING.md (setup & run), CONNECTING.md, HOSTING.md, INTEGRATION.md
tests/       guardrail, broker, signals, and review-workflow tests
config.yaml  caps, universe, cadence, paper/live switch, identity markers
```

## Status

The daily-review pipeline is built end-to-end: broker data access, identity verification,
the risk layer, the SQLite journal, the monitor signal-feed consumer, the daily-review
orchestration with the CAN SLIM skills wired in, and the HTML dashboard + chat-brief + push
delivery. Guardrail, broker, and signal logic are unit tested. It runs against a funded
paper account through the IBKR + FMP MCP connectors; the `ib_async` / IB-Gateway seam for
optional unattended live execution remains a documented `TODO(connector)` (see
[`docs/HOSTING.md`](docs/HOSTING.md) → Path B).
