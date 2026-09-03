from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

from .store import Store


class AuditTrail:
    def __init__(self, store: Store) -> None:
        self.store = store

    def append(self, *, session_id: str, agent: str, action: str, reasoning: str,
               outcome: str = "ok", gated: bool = False, result: object = None) -> dict:
        row = {"id": f"audit_{uuid4().hex[:12]}", "timestamp": datetime.now(UTC).isoformat(),
               "session_id": session_id, "agent": agent, "action": action,
               "reasoning": reasoning, "outcome": outcome, "gated": gated, "result": result}
        self.store.execute("INSERT INTO audit VALUES (?,?,?,?,?,?,?,?,?)", (
            row["id"], row["timestamp"], session_id, agent, action, reasoning, outcome,
            int(gated), self.store.dump(result)))
        return row

    def list(self, *, agent: str | None = None, limit: int = 200) -> list[dict]:
        sql, params = "SELECT * FROM audit", ()
        if agent:
            sql, params = sql + " WHERE agent=?", (agent,)
        rows = self.store.rows(sql + " ORDER BY timestamp DESC LIMIT ?", params + (min(limit, 500),))
        for row in rows:
            row["gated"] = bool(row["gated"])
            row["result"] = json.loads(row.pop("result_json"))
        return rows
