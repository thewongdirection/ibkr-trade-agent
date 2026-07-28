# Running the agent — explicit setup & run guide

This is the **authoritative, step-by-step** guide to running `ibkr-trade-agent`. If you read
only one doc, read this one. It covers **exactly which connectors to attach, where, and how to
run the review** — both by hand and on the daily schedule.

> **Safety, up front:** the agent is decision-support only. It **stages** orders for your
> one-tap approval and **never executes a trade**. It ships in **paper** mode. Going live
> takes two independent switches (see [§6](#6-paper-vs-live)).

---

## The two connectors you MUST attach

The agent has no broker credentials of its own. It reaches your account and market data
entirely through **two Claude MCP connectors**. Attach **both**:

| Connector | What it's for | Required? |
|---|---|---|
| **Interactive Brokers (IBKR)** | Read your account/positions/trades **and** stage orders for approval | **Required** — nothing runs without it |
| **Financial Modeling Prep (FMP)** | Fundamentals + quotes that feed the CAN SLIM grading/screening | **Required** for grading & new-idea screening |

Optional / future: **Gmail** (only if you later want the brief emailed). Not needed for the
core daily run.

> There are no API keys for IBKR or FMP anywhere in this repo — authorization happens in
> Claude's connector OAuth flow, out of band. The only secret this project references is
> `ANTHROPIC_API_KEY` in a git-ignored `.env`. See [CONNECTING.md](CONNECTING.md).

---

## Where to attach them — this matters

There are **two different places** a connector can be authorized, and the daily bot needs it
in **both**:

1. **Your interactive Claude account** — claude.ai → **Settings → Connectors** → connect
   **Interactive Brokers** and **Financial Modeling Prep**, completing each OAuth login.
   This is enough to run the review **by hand** in a chat.

2. **The scheduled Routine itself** — a Routine fires a **fresh, headless session** that does
   **not** inherit your interactive connectors automatically. You must **attach the connectors
   to the Routine** so the scheduled run can reach them. In claude.ai → **Routines** → open the
   Routine → **attach the Interactive Brokers and Financial Modeling Prep connectors** → save.

   > **This is the #1 thing that breaks a scheduled run.** If a firing reports "connector
   > unavailable," it's almost always because the connectors are authorized for your account
   > (place 1) but not delegated to the Routine (place 2). Attach them to the Routine and
   > re-fire.

A quick way to confirm the Routine can see them: fire it once manually (ask the assistant to
"fire the daily Routine as a test") and read the chat brief — step 1 of the run verifies the
IBKR connection and reports the effective mode.

---

## 1. Install

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .
```

## 2. Pull the CAN SLIM analysis skills

The grading/screening "brain" lives in two Claude Skills. Clone them into `./skills`:

```bash
scripts/setup_skills.sh
```

This populates `skills/can-slim-recommend` and `skills/can-slim-grader` (git-ignored). The
run loads `SKILL.md` from each. In a hosted Routine environment these must be present too —
commit them as submodules or run the setup script in the environment's setup step.

## 3. Configure

```bash
cp .env.example .env      # add ANTHROPIC_API_KEY; leave IBKR_ALLOW_LIVE unset
$EDITOR config.yaml       # review caps; keep account.mode: paper
```

Set your identity marker so a run against the wrong account aborts. Run
`python -m broker.session` once to see your account fingerprint, then in `config.yaml`:

```yaml
account:
  verify:
    expected_base_currency: "SGD"   # your account's base currency
    label: "my-ibkr-margin"         # a nickname only you recognize (optional)
```

## 4. Verify guardrails and connection

```bash
pytest -q                       # risk-layer + broker unit tests must pass
python -m broker.session        # connection OK? identity VERIFIED? correct mode?
python -m broker.account --all  # do the balances/positions look like yours?
```

## 5. Run the review

There are two ways to run it: **by hand** (a chat session) and **on a schedule** (the Routine).

### A. By hand — in a Claude chat

With the IBKR + FMP connectors attached to your account (place 1 above), just ask the
assistant to **"run the daily IBKR review"**, or paste the standalone instruction from
[`workflows/ROUTINE_PROMPT.md`](../workflows/ROUTINE_PROMPT.md). It will verify → read the
account → grade → screen → risk-check → stage → deliver the dashboard + brief.

### B. The Python entry point (dry-run / plumbing)

```bash
python -m workflows.daily_review              # dry run — grades & sizes, stages nothing
python -m workflows.daily_review --stage      # stage accepted orders for approval
python -m workflows.daily_review --out dash.html   # also write the HTML dashboard to a file
```

`--stage` is what turns proposals into one-tap approval orders in IBKR. Without it the run is
a dry run.

### C. On a schedule — the daily Routine (recommended)

This is the intended production path (zero infra). See [HOSTING.md](HOSTING.md) → **Path A**
for the full rationale. In short:

1. Attach **IBKR + FMP** to your account **and to the Routine** (see "Where to attach them").
2. Ensure the CAN SLIM skills are available in the Routine's environment (step 2).
3. Create the Routine pointing at [`workflows/ROUTINE_PROMPT.md`](../workflows/ROUTINE_PROMPT.md)
   — or just ask the assistant to **"set up the daily Routine."**
4. **Schedule (cron is UTC):** US pre-market **08:30 ET** = **`30 12 * * 1-5`** during EDT
   (Mar–Nov), **`30 13 * * 1-5`** during EST (Nov–Mar). Weekdays only.
5. Let the first few runs go on **paper** and watch them end-to-end before trusting it.

Each firing verifies the account, grades holdings, hunts ideas, risk-checks, **stages** orders
for your approval, and delivers a **dashboard + chat brief + completion push**.

## 6. Paper vs live

Default is **paper**. Going live requires **two independent switches** so it can't happen by
accident:

```bash
python -m broker.mode status    # show config mode, env interlock, effective mode
python -m broker.mode live      # switch 1: sets account.mode: live in config.yaml
export IBKR_ALLOW_LIVE=1        # switch 2: arms the env interlock
python -m broker.mode paper     # revert to safe default anytime
```

If only one switch is set, the effective mode stays **paper**. Even in live mode every order
is staged for your approval — there is no auto-execute code path.

## 7. Change how often it runs (daily → up to monthly)

The cadence lives in `config.yaml → schedule`. Default is **daily** (weekdays, pre-market).
Supported `frequency` values: **`daily`**, **`weekly`**, **`biweekly`**, **`monthly`**.

```yaml
schedule:
  frequency: daily          # daily | weekly | biweekly | monthly
  run_time: "08:30"         # local wall-clock time, HH:MM
  timezone: America/New_York
  day_of_week: mon          # for weekly/biweekly
  day_of_month: 1           # for monthly (1-28)
  cron: ""                  # optional explicit UTC cron override
```

After editing, get the exact **UTC cron** to put in your Routine:

```bash
python -m agent.schedule
# Cadence     : weekly
# Runs        : every week on Mon at 08:30 America/New_York (12:30 UTC)
# Cron (UTC)  : 30 12 * * 1
```

Then update the Routine's schedule to that cron (ask the assistant to "change the review to
weekly" and it will re-derive and update the Routine).

Notes:
- **Cron is UTC and the US pre-market offset shifts with daylight saving** (12:30 UTC in
  summer, 13:30 in winter). Re-run `python -m agent.schedule` after a clock change, or set an
  explicit `schedule.cron`.
- **Biweekly** can't be expressed in plain cron, so it fires a *weekly* cron and the run
  **self-gates to even ISO weeks** — on an off week it stages nothing and says so. Run the
  Python entry point (or the Routine, which honours the gate) rather than a raw cron if you
  rely on this.
- Changing the cadence does **not** change what a run does — only how often it happens.

## 8. Also get the brief on Telegram (optional)

Beyond the Claude chat + push, the brief can be delivered to a **Telegram chat** you control.
It's opt-in and needs no code changes — just two environment variables:

1. In Telegram, message **@BotFather** → `/newbot` → copy the **bot token**.
2. Message your new bot once, then open
   `https://api.telegram.org/bot<token>/getUpdates` and copy your **chat id**.
3. Set both (in `.env` for local runs, **and in the Routine's environment** for the scheduled
   bot — the scheduled session doesn't read your local `.env`):

   ```bash
   TELEGRAM_BOT_TOKEN=123456:ABC-your-token
   TELEGRAM_CHAT_ID=987654321
   ```

That's it. When both are set, every run also sends the brief to that chat via
`reporting.notify.deliver_brief()`. When they're unset it's a silent no-op, and a Telegram
outage never fails the review (delivery errors are logged, not raised). Secrets stay out of
git — `.env` is git-ignored and only `.env.example` placeholders are tracked.

---

## What each daily run does (the 7 steps)

1. **Verify** the IBKR connection and confirm identity (`account.verify`). Connector down or
   identity mismatch → stop, stage nothing, report it. Report effective mode.
2. **Read** balances + positions. Unfunded → informational only, no orders.
3. **Grade holdings** — market direction (M) first, then `can-slim-grader` on every position →
   hold / trim / exit, honoring the 7–8% stop (3% in a correction), flagging options near expiry.
4. **Hunt ideas** from two sources — `can-slim-recommend` (screen) + the monitor signal feed if
   configured. Grade each; only BUY-RANGE names with a valid pivot survive.
5. **Size + risk-check** every buy against `config.yaml` caps (notional, position/sector weight,
   cash buffer, max new positions). Reject breaches. Attach a stop to every entry.
6. **Stage** survivors with `create_order_instruction` for one-tap approval; **journal** every
   grade, staged order, and rejection.
7. **Deliver** — HTML dashboard + concise chat brief + completion push (with the count of
   actions awaiting your approval).

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Scheduled run says **"IBKR connector unavailable"** | Connectors attached to your account but **not to the Routine** | Attach IBKR + FMP to the Routine (place 2), re-fire |
| Run **aborts on identity** | Connected account ≠ `account.verify` marker | Confirm it's your account via `broker.session`; fix `expected_base_currency`/`label` |
| **No new ideas / grading errors** | FMP connector missing, or CAN SLIM skills not installed | Attach FMP; run `scripts/setup_skills.sh` |
| Orders **not appearing for approval** | Ran without `--stage`, or in dry-run chat | Re-run with `--stage` (or tell the assistant to stage) |
| Wrong **run time** | cron is UTC and the ET offset shifts with DST | Use `30 12 * * 1-5` (EDT) / `30 13 * * 1-5` (EST) |

---

## See also

- [CONNECTING.md](CONNECTING.md) — how the IBKR connection works, no-credentials policy, identity verification
- [HOSTING.md](HOSTING.md) — the three hosting paths (Routine / VM / GitHub Actions) in depth
- [INTEGRATION.md](INTEGRATION.md) — the stock-movement-monitor signal-feed contract
- [`workflows/ROUTINE_PROMPT.md`](../workflows/ROUTINE_PROMPT.md) — the exact standalone prompt the Routine fires
