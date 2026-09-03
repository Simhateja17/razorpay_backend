from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from threading import RLock
from typing import Any


class Store:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = str(path or Path(__file__).with_name("cartisan.db"))
        self._lock = RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript("""
        CREATE TABLE IF NOT EXISTS carts(session_id TEXT, product_id TEXT, quantity INTEGER NOT NULL,
          PRIMARY KEY(session_id, product_id));
        CREATE TABLE IF NOT EXISTS orders(id TEXT PRIMARY KEY, session_id TEXT NOT NULL, status TEXT NOT NULL,
          amount INTEGER NOT NULL, payment_link_id TEXT, payment_url TEXT, payload TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS approvals(id TEXT PRIMARY KEY, kind TEXT NOT NULL, target_id TEXT,
          before_json TEXT NOT NULL, after_json TEXT NOT NULL, reasoning TEXT NOT NULL, status TEXT NOT NULL,
          created_at TEXT NOT NULL, decided_at TEXT);
        CREATE TABLE IF NOT EXISTS audit(id TEXT PRIMARY KEY, timestamp TEXT NOT NULL, session_id TEXT NOT NULL,
          agent TEXT NOT NULL, action TEXT NOT NULL, reasoning TEXT NOT NULL, outcome TEXT NOT NULL,
          gated INTEGER NOT NULL, result_json TEXT NOT NULL);
        """)
        self._db.commit()

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._db.execute(sql, params)
            self._db.commit()
            return cur

    def rows(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, params).fetchall()]

    @staticmethod
    def dump(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def load(value: str) -> Any:
        return json.loads(value)
