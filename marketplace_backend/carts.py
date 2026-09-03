"""Optimistic-concurrency and replay protection for cart-shaped writes.

The cart repository that used to live here is gone. It was keyed by the legacy flat
`products` table and held stock out of `products.stock` the moment a line was added,
which ADR 0012 forbids: a cart is an intention, and only a confirmed checkout is a
claim on stock. The one cart is now `cartisan_agent.core_port.CoreCommercePort` —
variant-keyed, reserving nothing — and the browser and the agent both go through it.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable

from .store import Store


class ConflictError(Exception):
    """The caller's expected cart version is stale; it must re-read before mutating."""


class IdempotencyLedger:
    """Replay protection for consequential effects (ADR 0029).

    A repeated key with the same request returns the first recorded response; the
    same key with a different request is a conflict, because the caller is reusing
    a key that already committed to a different effect.
    """

    def __init__(self, store: Store) -> None:
        self.store = store

    @staticmethod
    def fingerprint(payload: dict) -> str:
        parts = "|".join(f"{k}={payload[k]!r}" for k in sorted(payload))
        return hashlib.sha256(parts.encode()).hexdigest()

    def run(self, *, principal_id: str, operation: str, key: str | None, request: dict,
            effect: Callable[[], dict]) -> dict:
        if not key:
            return effect()
        fingerprint = self.fingerprint(request)
        rows = self.store.rows(
            "SELECT operation,request_fingerprint,response_json FROM idempotency_records "
            "WHERE key=? AND principal_id=?",
            (key, principal_id),
        )
        if rows:
            record = rows[0]
            if record["operation"] != operation or record["request_fingerprint"] != fingerprint:
                raise ConflictError("This idempotency key was already used for a different request")
            return self.store.load(record["response_json"])
        response = effect()
        self.store.execute(
            "INSERT INTO idempotency_records (key,principal_id,operation,request_fingerprint,response_json) "
            "VALUES (?,?,?,?,?)",
            (key, principal_id, operation, fingerprint, self.store.dump(response)),
        )
        return response
