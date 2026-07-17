"""SQLite-backed journal for runs, grades, and decisions.

Pure standard library. The journal is the agent's memory: each weekly run opens a row in
``runs`` and records every grade and every staged/rejected order against it, so the next run
can read back why each position is held.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Journal:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_schema(self) -> None:
        with closing(self._connect()) as conn:
            conn.executescript(SCHEMA_PATH.read_text())
            conn.commit()

    # --- writes -----------------------------------------------------------------

    def start_run(
        self,
        mode: str,
        market_read: str = "",
        equity: float = 0.0,
        cash: float = 0.0,
        notes: str = "",
    ) -> int:
        with closing(self._connect()) as conn:
            cur = conn.execute(
                "INSERT INTO runs (started_at, mode, market_read, equity, cash, notes) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (_now(), mode, market_read, equity, cash, notes),
            )
            conn.commit()
            return int(cur.lastrowid)

    def record_grade(
        self,
        run_id: int,
        symbol: str,
        verdict: str,
        score_70: int | None,
        letters: dict[str, int] | None,
        summary: str = "",
    ) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                "INSERT INTO grades (run_id, symbol, verdict, score_70, letters_json, "
                "summary, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    symbol,
                    verdict,
                    score_70,
                    json.dumps(letters or {}),
                    summary,
                    _now(),
                ),
            )
            conn.commit()

    def record_decision(
        self,
        run_id: int,
        symbol: str,
        action: str,
        disposition: str,
        *,
        quantity: float | None = None,
        limit_price: float | None = None,
        stop_price: float | None = None,
        notional: float | None = None,
        asset_class: str | None = None,
        sector: str | None = None,
        rationale: str = "",
        reject_reason: str = "",
    ) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                "INSERT INTO decisions (run_id, symbol, action, disposition, quantity, "
                "limit_price, stop_price, notional, asset_class, sector, rationale, "
                "reject_reason, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    symbol,
                    action,
                    disposition,
                    quantity,
                    limit_price,
                    stop_price,
                    notional,
                    asset_class,
                    sector,
                    rationale,
                    reject_reason,
                    _now(),
                ),
            )
            conn.commit()

    # --- reads ------------------------------------------------------------------

    def latest_grade(self, symbol: str) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM grades WHERE symbol = ? ORDER BY id DESC LIMIT 1",
                (symbol,),
            ).fetchone()
            return dict(row) if row else None

    def decisions_for_run(self, run_id: int) -> list[dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM decisions WHERE run_id = ? ORDER BY id", (run_id,)
            ).fetchall()
            return [dict(r) for r in rows]

    def position_history(self, symbol: str) -> list[dict[str, Any]]:
        """Every decision ever recorded for a symbol — the 'why do I hold this' trail."""
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT d.*, r.started_at AS run_started, r.mode "
                "FROM decisions d JOIN runs r ON r.id = d.run_id "
                "WHERE d.symbol = ? ORDER BY d.id",
                (symbol,),
            ).fetchall()
            return [dict(r) for r in rows]

    def record_decisions(self, run_id: int, rows: Iterable[dict[str, Any]]) -> None:
        for row in rows:
            self.record_decision(run_id, **row)
