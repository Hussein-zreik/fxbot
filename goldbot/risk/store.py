"""SQLite state for the risk core (CLAUDE.md section 9): halts, decisions, trades, kv.

Halt and ladder state must survive restarts, so everything lives here. The file
is opened in WAL mode; all access is serialised by one lock.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS halts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, kind TEXT NOT NULL,
    reason TEXT NOT NULL, cleared_ts TEXT, cleared_note TEXT);
CREATE TABLE IF NOT EXISTS risk_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, intent_id TEXT, strategy TEXT,
    symbol TEXT, side TEXT, approved INTEGER NOT NULL, reason_code TEXT NOT NULL,
    detail TEXT, size REAL);
CREATE TABLE IF NOT EXISTS trade_log (
    ticket INTEGER PRIMARY KEY, day_open TEXT NOT NULL, day_close TEXT, risk_usd REAL,
    lots REAL, profit REAL, r_multiple REAL, closed INTEGER NOT NULL DEFAULT 0);
"""


class RiskStore:
    def __init__(self, path: str):
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(_SCHEMA)
            self._db.commit()

    # ------------------------------------------------------------- generic
    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._db.execute(sql, params)
            self._db.commit()
            return cur

    def query(self, sql: str, params: tuple = ()) -> List[sqlite3.Row]:
        with self._lock:
            return list(self._db.execute(sql, params).fetchall())

    # ------------------------------------------------------------- key/value
    def get(self, key: str, default: Any = None) -> Any:
        rows = self.query("SELECT value FROM kv WHERE key=?", (key,))
        return json.loads(rows[0]["value"]) if rows else default

    def set(self, key: str, value: Any) -> None:
        self.execute("INSERT INTO kv(key, value) VALUES(?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (key, json.dumps(value)))

    # ------------------------------------------------------------- halts
    def add_halt(self, kind: str, reason: str, ts: datetime) -> int:
        return self.execute("INSERT INTO halts(ts, kind, reason) VALUES(?, ?, ?)",
                            (ts.isoformat(), kind, reason)).lastrowid

    def active_halts(self, kind: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM halts WHERE cleared_ts IS NULL"
        params: tuple = ()
        if kind:
            sql, params = sql + " AND kind=?", (kind,)
        return [dict(r) for r in self.query(sql + " ORDER BY id", params)]

    def clear_halts(self, kind: str, note: str, ts: datetime) -> None:
        self.execute("UPDATE halts SET cleared_ts=?, cleared_note=? "
                     "WHERE kind=? AND cleared_ts IS NULL", (ts.isoformat(), note, kind))

    # ------------------------------------------------------------- decisions
    def record_decision(self, ts: datetime, intent_id: str, strategy: str, symbol: str,
                        side: str, approved: bool, reason_code: str, detail: str,
                        size: Optional[float]) -> None:
        self.execute("INSERT INTO risk_decisions(ts, intent_id, strategy, symbol, side, "
                     "approved, reason_code, detail, size) VALUES(?,?,?,?,?,?,?,?,?)",
                     (ts.isoformat(), intent_id, strategy, symbol, side, int(approved),
                      reason_code, detail, size))

    def decisions(self, limit: int = 20) -> List[Dict[str, Any]]:
        return [dict(r) for r in self.query(
            "SELECT * FROM risk_decisions ORDER BY id DESC LIMIT ?", (limit,))]
