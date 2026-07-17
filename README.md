# ibkr-trade-agent

An assistant that manages an **Interactive Brokers (IBKR)** account: it reviews your
portfolio on a schedule, hunts for new positions and manages existing ones using the
**CAN SLIM** growth-investing methodology, and stages trades for your **one-click
approval**. It never executes a trade on its own.

> **Not investment advice.** This software is decision-support tooling. It pairs every
> idea with a loss-cutting exit rule, but markets carry risk of loss and you are
> responsible for every order you approve. Start on a **paper account**.

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
                       │  workflows/weekly    the scheduled review      │
                       │                                               │
                       │  → stages orders via create_order_instruction  │
                       │    for your ONE-CLICK APPROVAL (never auto)    │
                       └─────────────────────────────────────────────┘
```

- **The brain** — the [`can-slim-recommend`](https://github.com/thewongdirection/can-slim-recommend)
  and [`can-slim-grader`](https://github.com/thewongdirection/can-slim-grader) Claude Skills.
  They are strictly read-only: they screen the market and grade tickers against CAN SLIM but
  **never** call order or account tools. `recommend` finds *new* ideas; `grader` judges a
  *named* holding (used weekly to decide hold / trim / exit).
- **The hands + risk** — this repo. It reads your positions and balances, runs the weekly
  review, applies the CAN SLIM output through a **risk layer** (position and per-order caps,
  a hard paper/live switch), stages orders for approval, and journals every decision with its
  rationale so each week's run has memory of *why* you hold what you hold.

### Technology stack

| Concern            | Choice                                   | Why |
|--------------------|------------------------------------------|-----|
| Language           | Python 3.11+                             | Best financial ecosystem |
| Agent runtime      | [Claude Agent SDK](https://docs.claude.com/en/api/agent-sdk) | Native MCP; the IBKR + FMP connectors plug straight in |
| Broker connection  | IBKR MCP connector                       | No infra to run; order-instruction flow gives the approval gate for free |
| Market data        | FMP + IBKR (+ optional Massive)          | Fundamentals + technicals for the CAN SLIM "blend" |
| Analysis           | `can-slim-recommend` / `can-slim-grader` | The scoring brain |
| Scheduling         | Routine / cron → `workflows/weekly_review.py` | The weekly cadence |
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

```bash
# Verify the IBKR connection + that it's YOUR account (fingerprint + identity check):
python -m broker.session

# Retrieve account info, positions, open orders, and trade history:
python -m broker.account --all
python -m broker.account --trades YEAR_TO_DATE

# Check / switch paper<->live mode (see "Safety model" below):
python -m broker.mode status
python -m broker.mode live      # + export IBKR_ALLOW_LIVE=1 to actually arm live

# One weekly review on demand (stages nothing without --stage):
python -m workflows.weekly_review

# Stage the approved-shape orders for one-click approval in IBKR:
python -m workflows.weekly_review --stage
```

See [`docs/CONNECTING.md`](docs/CONNECTING.md) for how the IBKR connection works, why no
credentials live in this repo, and how to verify the connected account is yours.

### Account login & identity

There is no password to store: the IBKR MCP connector is authorized via Claude's connector
OAuth, so "login" here means verifying the connector is reachable and identifying the
account. The connector **masks the account number and owner name**, so `python -m broker.session`
establishes identity from a fingerprint you recognize (base currency, account inception date,
positions, recent trades) and an optional marker you set in `config.yaml → account.verify`.
With a marker set, the agent refuses to operate if the live account doesn't match.

To run it automatically every week, point a Routine / cron entry at the same command — see
`workflows/weekly_review.py` for the schedule contract.

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
journal/     SQLite trade + rationale log
workflows/   weekly_review.py  (the scheduled entry point)
skills/      CAN SLIM skills (populated by scripts/setup_skills.sh; git-ignored)
scripts/     setup_skills.sh
docs/        CONNECTING.md (IBKR connection, no-credentials policy, verification)
tests/       guardrail, broker, and weekly-review tests
config.yaml  caps, universe, cadence, paper/live switch, identity markers
```

## Status

This is a **scaffold**: structure, config, safety layer, journal, and the weekly-review
orchestration are in place with the CAN SLIM skills wired in. The guardrail logic is unit
tested. The live Agent-SDK loop and IBKR order staging are stubbed at the connector
boundary and marked with `TODO(connector)` — they need a session with the IBKR/FMP MCP
servers attached to run end to end.
