# Connecting the agent to Interactive Brokers

## No credentials live in this repo — by design

This project stores **no** IBKR username, password, account number, session token, or API
key, and it never will. The only secret it references is your **`ANTHROPIC_API_KEY`**, which
goes in a local `.env` file that is git-ignored (`.gitignore` excludes `.env`). The tracked
`.env.example` contains **placeholders only**.

The agent reaches IBKR through the **IBKR MCP connector**, which is authorized out-of-band
via Claude's connector OAuth flow — not with credentials this codebase holds. There is no
password for the code to store because the code never sees one.

## How the connection actually works

```
  You ── authorize once ──► Claude IBKR connector (OAuth) ──► your IBKR account
                                     ▲
   ibkr-trade-agent ── MCP tool calls (read-only + order staging) ──┘
```

1. In Claude, connect the **Interactive Brokers** connector and complete IBKR's OAuth login.
   This is where authentication happens — in IBKR's own flow, not here.
2. The connector then exposes read-only market/account tools and the order-**staging** tool
   to the agent. The agent calls those tools; it never handles your login.
3. Approve or reject every staged order yourself in IBKR. The agent cannot execute a trade.

## Verify it's *your* account

The connector **masks the account number and owner name** (privacy by design — it never
returns a `U#######` id or the holder's name). So verify by fingerprint instead:

```bash
python -m broker.session
```

This prints base currency, **account inception date**, funding, positions, and recent
trades — signals only you would recognize. Once you confirm it's yours, lock it in by setting
non-sensitive markers in `config.yaml` (never a number or credential):

```yaml
account:
  verify:
    expected_base_currency: "SGD"   # your account's base currency
    label: "my-ibkr-margin"         # a nickname you recognize
```

With a marker set, the agent refuses to operate if the connected account doesn't match.

## Paper (default) vs live money

The default is **paper trading**. Going live needs **two independent switches** so it can
never happen by accident:

```bash
python -m broker.mode status     # show config mode, env interlock, and effective mode
python -m broker.mode live       # switch 1: set account.mode: live in config.yaml
export IBKR_ALLOW_LIVE=1         # switch 2: arm the env interlock in your shell
python -m broker.mode paper      # revert to the safe default anytime
```

If only one switch is set, the effective mode stays **paper**. Even in live mode, every
order is staged for your one-click approval — the agent never auto-executes.

## What to check before trusting a run

- `python -m broker.session` → connection OK, identity VERIFIED, correct mode.
- `python -m broker.account` → the balances/positions you expect.
- `git status` / `git grep -i` for secrets → nothing but `.env.example` placeholders tracked.
