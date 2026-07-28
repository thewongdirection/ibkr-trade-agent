# Decision records

Short notes on *why* the code looks the way it does — especially where the answer isn't obvious
from reading it. If you're about to delete something that appears unused, check here first.

---

## ADR-001 — Keep a self-hosted IB Gateway transport (`broker/gateway.py`)

**Date:** 2026-07-28 · **Status:** accepted, inactive by default

### The question this answers
`broker/gateway.py` is ~400 lines that **nothing runs by default** (`gateway.transport: mcp`).
It looks like dead code. Why is it here?

### Context
The bot's normal transport is the Claude **IBKR MCP connector**. It works perfectly in an
interactive session — an account read returns instantly. But a **scheduled Routine fires a
headless session**, and there the connector never answers.

This was not a guess. Scheduled runs kept delivering their first Telegram checkpoint and then
going completely silent, so we bisected with a probe that pinged Telegram before and after
every step:

| Probe step | Delivered? |
|---|---|
| `D1 alive` | ✅ |
| `D2 second bash OK` | ✅ — so **not** a turn/budget limit |
| `D3 about to call IBKR` | ✅ |
| **`D4 IBKR result`** | ❌ **never** |

The run ends at the connector call. A hung MCP call **cannot be timed out or caught from
inside the session** — control never comes back — so the session simply dies, silently.

Ruled out along the way, each by a separate fix or test:
- *Delivery* — Telegram worked throughout (every ping arrived).
- *Session limits* — `D2` proves multiple commands run fine.
- *Skills / network* — fixed separately by allowlisting `github.com`; runs report
  `skills: {'can-slim-recommend': True, 'can-slim-grader': True}`.
- *A hanging `git clone`* — removed from the scheduled path entirely; runs still died.

### Decision
Keep a **second transport** that doesn't depend on the connector: a direct socket to a locally
running **IB Gateway / TWS** via `ib_async`. It implements the same
`broker.client.BrokerClient` protocol, so choosing between them is a config line
(`gateway.transport: mcp | gateway`), not a code change.

### Consequences
- **It is the only way to get account data in an unattended run.** Without it, a scheduled run
  can produce a market read and a CAN SLIM screen, but not your positions.
- **It costs nothing when unused.** `ib_async` is an optional extra (`pip install -e ".[gateway]"`)
  and the import is deferred, so the package and the full test suite work without it installed.
- **It is covered by tests** (15, against an `ib_async`-shaped fake — no gateway, no network),
  so it won't quietly rot.
- Using it is an **operational** commitment, not a code one: a VM, IB Gateway kept logged in via
  IBC, and IBKR's daily re-auth + 2FA. See [HOSTING.md](HOSTING.md) → Path B.
- Safety is *stronger* on this path: orders are staged with `transmit=False` (IBKR's native
  stage-for-review), and a paper config physically refuses to connect to a live port.

### When to revisit
- **If a scheduled run ever gets past `CHECKPOINT B`** — the connector has started responding
  headlessly, and the gateway becomes optional rather than the only route. No code change
  needed either way; re-test occasionally.
- **If you decide unattended runs aren't worth the ops burden** and are happy to trigger the
  review interactively (which works completely today), then this module can be deleted. That is
  the *only* condition under which deleting it is the right call.

---

## ADR-002 — One-way Telegram; no command surface

**Date:** 2026-07-28 · **Status:** accepted

### Context
The bot briefly grew a two-way Telegram surface — a `getUpdates` poller, a command router, and
IBKR watchlist create/edit/delete — so you could text it `/account` or `/watch NVDA`. In use,
the value didn't justify the surface area: it required a long-running poller for timely replies
(a Routine cron fires at most hourly), and it handed the agent **account-write** tools.

### Decision
The bot **sends** and does not listen. Each run delivers a short text summary plus the dashboard
as a PDF attachment (`reporting.notify.deliver_report`). Removed: `reporting/telegram_bot.py`,
`reporting/commands.py`, `broker/watchlist.py`, the watchlist write-tool allowlist in
`agent/runtime.py`, and `reporting/account_brief.py`.

### Consequences
- **The only write the agent can perform is staging an order for your approval.** Every other
  IBKR tool it holds is read-only.
- Ad-hoc questions are answered by asking in a Claude chat, where the connector works — no
  bespoke command parser needed.
- `broker/gateway.py` and `signals/` were **deliberately kept** through this cleanup: the first
  per ADR-001, the second because the monitor signal feed is a candidate source for the review
  itself, not a command-era addition.
