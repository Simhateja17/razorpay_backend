from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from threading import RLock
from typing import Any


_TABLE_NAMES = ("carts", "orders", "approvals", "audit")


class Store:
    """Small persistence boundary with SQLite for tests and Supabase Postgres in runtime."""

    def __init__(self, path: str | Path | None = None, database_url: str | None = None) -> None:
        self._lock = RLock()
        self._pool = None
        self._db = None

        # An explicit path always selects SQLite. This keeps tests isolated even if
        # the developer shell has SUPABASE_DATABASE_URL configured.
        if database_url and path is None:
            self.backend = "supabase"
            self.path = None
            self._connect_postgres(database_url)
        else:
            self.backend = "sqlite"
            self.path = str(path or Path(__file__).with_name("cartisan.db"))
            self._connect_sqlite()

    def _connect_sqlite(self) -> None:
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

    def _connect_postgres(self, database_url: str) -> None:
        try:
            from psycopg.rows import dict_row
            from psycopg_pool import ConnectionPool
        except ImportError as exc:
            raise RuntimeError(
                "Supabase is configured but the Postgres driver is missing. "
                "Run: pip install -r requirements.txt"
            ) from exc

        self._pool = ConnectionPool(
            conninfo=database_url,
            min_size=1,
            max_size=5,
            timeout=10,
            kwargs={"autocommit": True, "row_factory": dict_row},
            open=True,
        )
        self._pool.wait(timeout=10)
        self.rows("SELECT 1 AS ready")

    def _postgres_sql(self, sql: str) -> str:
        rewritten = sql.replace("?", "%s").replace("MIN(%s,carts.quantity+%s)", "LEAST(%s,carts.quantity+%s)")
        for table in _TABLE_NAMES:
            rewritten = re.sub(rf"\b{table}\b", f"cartisan.{table}", rewritten)
        return rewritten

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> Any:
        if self.backend == "supabase":
            with self._pool.connection() as connection:
                return connection.execute(self._postgres_sql(sql), params)
        with self._lock:
            cursor = self._db.execute(sql, params)
            self._db.commit()
            return cursor

    def rows(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        if self.backend == "supabase":
            with self._pool.connection() as connection:
                return list(connection.execute(self._postgres_sql(sql), params).fetchall())
        with self._lock:
            return [dict(row) for row in self._db.execute(sql, params).fetchall()]

    def close(self) -> None:
        if self._pool is not None:
            self._pool.close()
        if self._db is not None:
            self._db.close()

    @staticmethod
    def dump(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def load(value: str) -> Any:
        return json.loads(value)
