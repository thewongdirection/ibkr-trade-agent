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

> ### ⚠️ Known limitation — the IBKR connector in a *scheduled* session
> A Routine fires a **headless** session, and in testing the **IBKR MCP connector never
> responded there**: the run sent its first checkpoint, called IBKR, and stopped. A hung MCP
> call cannot be timed out or recovered from inside the session, so the run ends silently.
>
> This was isolated with a bisect probe: Telegram delivery worked, multiple shell commands
> worked, and the CAN SLIM skills installed — but the first IBKR call never returned. The same
> connector works fine in an **interactive** chat session (the account reads instantly there),
> so it is specific to headless/scheduled runs, not to your credentials or config.
>
> **What still works on a schedule:** everything that doesn't touch IBKR — Telegram delivery,
> the skills, the dashboard render, the journal.
>
> **Options if you need an unattended review:**
> 1. **Run it interactively** — ask the assistant to "run the daily IBKR review" in a chat.
>    Fully working today; you just have to start it.
> 2. **Self-host via IB Gateway — implemented, see [§10](#10-self-hosted-transport-ib-gateway).**
>    `transport: gateway` swaps the MCP connector for a direct socket to a local IB Gateway,
>    which has no headless limitation. Real infrastructure, but it runs on its own.
> 3. **Keep the Routine for the non-IBKR half** — market screening and delivery on schedule,
>    with the account read done interactively.
>
> Worth re-testing occasionally: if a scheduled run ever gets past CHECKPOINT B, the connector
> has started working headlessly and the full pipeline resumes with no code change.
>
> The full evidence, and why the gateway transport is kept even though it's off by default, is
> recorded in [DECISIONS.md — ADR-001](DECISIONS.md).

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

Beyond the Claude chat + push, the brief can be delivered to a **Telegram chat** you control —
a phone alert independent of the Claude app. It's opt-in and needs no code changes, but it has
**one non-obvious requirement** most people miss: because the bot posts to Telegram with a
**direct outbound HTTPS call** (unlike the IBKR/FMP connectors, whose traffic is routed through
Anthropic), the scheduled environment's **egress proxy will block it unless you allowlist
`api.telegram.org`**. You need **all three** of the following, and they must live on the
**environment the Routine uses** (see the "Environments" box below).

### Step 1 — Create the bot and get your two values
1. In Telegram, message **@BotFather** → `/newbot` → copy the **bot token** (looks like
   `123456:ABC-DEF...`).
2. **Message your new bot once** (send it any text). A bot can never open a conversation with
   you, so without this first message every send fails with `chat not found`.
3. Open `https://api.telegram.org/bot<token>/getUpdates` in a browser and copy the
   `result[].message.chat.id` — that's your **chat id** (a number like `987654321`).

### Step 2 — Set the two environment variables **on the environment**
Set them in `.env` for local hand-runs, **and** on the scheduled Routine's environment
(claude.ai → the environment's **Environment variables**) — the scheduled session does **not**
read your local `.env`:

```bash
TELEGRAM_BOT_TOKEN=123456:ABC-your-token
TELEGRAM_CHAT_ID=987654321
```

Watch for stray spaces or wrapping quotes around the values — they cause silent failures.

### Step 3 — Allowlist `api.telegram.org` (the step everyone forgets)
On the **same environment**, open **Network access / Allowed domains** and add:

```
api.telegram.org
```

The policy must be the **custom allowlist** mode (not a "trusted-only" mode that ignores your
additions). Enter the bare host — `api.telegram.org`, **not** a full URL like
`https://api.telegram.org/...`. Without this, `getMe`/`sendMessage` fail with a proxy
`403 Forbidden` / "Tunnel connection failed" even though the token and chat id are perfect.

> ### 📦 Environments — where this config actually lives
> A **Routine fires a fresh, headless session in a specific *environment***, and that
> environment — **not** your interactive chat session — is what holds the env vars and the
> network allowlist. Two consequences that bite people:
> - **Your interactive chat can't validate this setup.** A normal chat runs in a *different*
>   environment, so it won't see the Routine's env vars and may 403 on Telegram even when the
>   Routine is configured correctly. Test by firing the Routine (or a one-off test) **in the
>   Routine's environment**, not by checking from a chat.
> - **Set the config on the *right* environment.** Confirm which environment your Routine uses
>   (ask the assistant to "show my Routine's environment") and put the two env vars + the
>   `api.telegram.org` allowlist **there**. Editing a different environment does nothing.
> - **Connectors travel with the Routine, not the environment.** If you move the Routine to a
>   different environment, the IBKR + FMP connectors stay attached — but you must re-do the
>   Telegram env vars + allowlist on the new environment (they're environment-scoped).

### Step 4 — Test it
Ask the assistant to **"fire a Telegram delivery test on the Routine's environment."** If the
test message lands in your chat, you're done. If not:
- **Proxy 403 / tunnel error** → `api.telegram.org` isn't allowlisted on that environment (Step 3).
- **`chat not found`** → you didn't message the bot first, or the chat id is wrong (Step 1).
- **Nothing at all** → the env vars aren't set on that environment (Step 2).

### How it behaves
When both vars are set, every run also sends the brief to that chat via
`reporting.notify.deliver_brief()` (wired in as **step 8** of the Routine prompt). When they're
unset it's a silent no-op, and a Telegram outage never fails the review — delivery errors are
logged, not raised. Secrets stay out of git: `.env` is git-ignored and only `.env.example`
placeholders are tracked. The **chat id does not change** if you swap the bot token later
(it identifies the chat, not the bot) — but a brand-new bot still needs the one-time
"message it once" from Step 1.

## 9. Telegram — a one-way update each run

The bot **sends you an update; it does not take commands.** Every run delivers two things:

1. a **short text summary** you can read on a lock screen — mode, market read, equity/cash,
   holdings actions, orders awaiting approval with a one-line CAN SLIM reason, and warnings;
2. the **full dashboard as a PDF attachment** (falls back to attaching the HTML when no
   browser is available to render it).

That's `reporting.notify.deliver_report()`, which the review calls for you:

```bash
daily-review --stage                      # runs, then sends summary + dashboard PDF
python -m reporting.notify "any message"  # send an ad-hoc line
```

Delivery never fails the run: a Telegram outage is logged rather than raised, and a missing
PDF converter only downgrades the attachment to HTML.

To inspect the account by hand outside a run:

```bash
account-summary            # summary + balances + positions + open orders
account-summary --all      # + 90-day trade history
```

---

## 10. Self-hosted transport (IB Gateway)

The bot can reach your account two ways. Which one it uses is `gateway.transport` in
`config.yaml` — everything downstream (review, risk caps, dashboard, journal) is identical.

| | `mcp` (default) | `gateway` |
|---|---|---|
| Reaches IBKR via | the Claude IBKR connector | a local **IB Gateway / TWS** socket (`ib_async`) |
| Interactive chat runs | ✅ works | ✅ works |
| **Scheduled/headless runs** | ❌ **connector doesn't respond** | ✅ works |
| Setup needed | attach the connector | a VM + IB Gateway kept logged in |

Use `gateway` when you want the review to run **without you**. Setup:

```bash
pip install -e ".[gateway]"        # installs ib_async
```
```yaml
gateway:
  transport: gateway   # was: mcp
  host: 127.0.0.1
  port: 4002           # Gateway paper 4002 | live 4001 | TWS paper 7497 | live 7496
  client_id: 17
  readonly: true       # set false only when you want the review to STAGE orders
```
```bash
gateway-check --check              # verify connectivity before scheduling anything
account-summary                    # now reads through the gateway
daily-review --stage               # stage orders into TWS for approval
```

**How "staging" works here.** Orders are placed with **`transmit=False`** — IBKR's native
stage-for-review. The order appears in TWS pre-filled and inert; it is not working, will never
fill on its own, and *you* press Transmit. `transmit=True` appears nowhere in this codebase.

**Three independent switches guard live trading:**
1. `account.mode: live` in config, **and**
2. `IBKR_ALLOW_LIVE=1` in the environment, **and**
3. a live **port** (4001/7496) — a paper config refuses to connect to a live port and vice
   versa, so a mistyped port can't reach your live account.

Plus `readonly: true` (the default) blocks order placement at the API level entirely.

Full VM/IBC/2FA walkthrough and the systemd timer: [HOSTING.md](HOSTING.md) → **Path B**.
Why this transport exists at all: [DECISIONS.md — ADR-001](DECISIONS.md).

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
| Scheduled run sends the **first checkpoint then goes silent** | The IBKR connector did not respond in the headless session — see the box below | Run the review interactively, or self-host ([HOSTING.md](HOSTING.md) Path B) |
| Skills missing **only in scheduled runs** | `github.com` not on that environment's allowlist, so the skills can't be cloned | Add `github.com` (bare host — **not** `www.github.com`) to Allowed domains |
| **No Telegram message**, proxy `403` / tunnel error | `api.telegram.org` not allowlisted on the Routine's environment | Add `api.telegram.org` (bare host) to that environment's Allowed domains ([§8](#8-also-get-the-brief-on-telegram-optional) Step 3) |
| **No Telegram message**, `chat not found` | Never messaged the bot, or wrong chat id | Message the bot once, re-read the id via `getUpdates` ([§8](#8-also-get-the-brief-on-telegram-optional) Step 1) |
| **No Telegram message**, nothing at all | `TELEGRAM_*` vars set on the wrong environment (or not at all) | Set both vars on the environment your Routine actually uses ([§8](#8-also-get-the-brief-on-telegram-optional) Step 2 + Environments box) |

---

## See also

- [CONNECTING.md](CONNECTING.md) — how the IBKR connection works, no-credentials policy, identity verification
- [HOSTING.md](HOSTING.md) — the three hosting paths (Routine / VM / GitHub Actions) in depth
- [INTEGRATION.md](INTEGRATION.md) — the stock-movement-monitor signal-feed contract
- [DECISIONS.md](DECISIONS.md) — why the code looks like this (read before deleting anything
  that appears unused)
- [`workflows/ROUTINE_PROMPT.md`](../workflows/ROUTINE_PROMPT.md) — the exact standalone prompt the Routine fires
