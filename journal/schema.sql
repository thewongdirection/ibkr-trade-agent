-- ibkr-trade-agent journal schema.
-- Every weekly run and every decision within it is recorded so future runs (and you) have
-- memory of WHY each position exists. Never store account-bound identifiers here.

CREATE TABLE IF NOT EXISTS runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at    TEXT NOT NULL,          -- ISO8601
    mode          TEXT NOT NULL,          -- paper | live
    market_read   TEXT,                   -- M verdict (uptrend / under pressure / correction)
    equity        REAL,
    cash          REAL,
    notes         TEXT
);

-- CAN SLIM grades for existing holdings and evaluated candidates.
CREATE TABLE IF NOT EXISTS grades (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        INTEGER NOT NULL REFERENCES runs(id),
    symbol        TEXT NOT NULL,
    verdict       TEXT NOT NULL,          -- BUY-RANGE | WATCH | AVOID
    score_70      INTEGER,
    letters_json  TEXT,                   -- {"C":8,"A":7,...}
    summary       TEXT,
    created_at    TEXT NOT NULL
);

-- Every order the agent staged OR rejected, with the reason.
CREATE TABLE IF NOT EXISTS decisions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        INTEGER NOT NULL REFERENCES runs(id),
    symbol        TEXT NOT NULL,
    action        TEXT NOT NULL,          -- BUY | SELL | TRIM | EXIT | HOLD
    disposition   TEXT NOT NULL,          -- staged | rejected | noted
    quantity      REAL,
    limit_price   REAL,
    stop_price    REAL,
    notional      REAL,
    asset_class   TEXT,
    sector        TEXT,
    rationale     TEXT,                   -- CAN SLIM-only reasoning
    reject_reason TEXT,                   -- populated when disposition = rejected
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_grades_symbol ON grades(symbol);
CREATE INDEX IF NOT EXISTS idx_decisions_symbol ON decisions(symbol);
CREATE INDEX IF NOT EXISTS idx_decisions_run ON decisions(run_id);
