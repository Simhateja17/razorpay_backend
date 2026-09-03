from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from .store import Store

MAX_QUANTITY = 10


class ConflictError(Exception):
    """The caller's expected cart version is stale; it must re-read before mutating."""


class CartRepository:
    """The one durable active cart per authenticated customer (ADR 0022).

    Ownership comes from the verified principal, never from a conversation or a
    client-supplied id, so the visible cart and the agent's `get_cart` always read
    the same row. Every mutation bumps `state_version`; a mutation that carries a
    stale expected version is a conflict rather than a silent overwrite.
    """

    def __init__(self, store: Store, products: Callable[[], dict[str, dict]]) -> None:
        self.store, self._products = store, products

    # ------------------------------------------------------------------ read

    def active_cart_id(self, customer_id: str) -> str:
        rows = self.store.rows(
            "SELECT id FROM customer_carts WHERE customer_id=? AND status='active'", (customer_id,)
        )
        if rows:
            return rows[0]["id"]
        cart_id = f"cart_{uuid4().hex[:12]}"
        # The partial unique index makes a concurrent create fail rather than
        # produce a second active cart; on that loss we re-read the winner.
        try:
            self.store.execute(
                "INSERT INTO customer_carts (id,customer_id,status,state_version) VALUES (?,?,'active',0)",
                (cart_id, customer_id),
            )
        except Exception:
            rows = self.store.rows(
                "SELECT id FROM customer_carts WHERE customer_id=? AND status='active'", (customer_id,)
            )
            if not rows:
                raise
            return rows[0]["id"]
        return cart_id

    def read(self, customer_id: str) -> dict:
        cart_id = self.active_cart_id(customer_id)
        return self._render(cart_id, customer_id)

    def _render(self, cart_id: str, customer_id: str) -> dict:
        version_rows = self.store.rows("SELECT state_version FROM customer_carts WHERE id=?", (cart_id,))
        rows = self.store.rows(
            "SELECT product_id,quantity FROM cart_lines WHERE cart_id=? ORDER BY product_id", (cart_id,)
        )
        products = self._products()
        lines, total = [], 0
        for row in rows:
            product = products.get(row["product_id"])
            if not product:
                continue
            amount = product["price"] * row["quantity"]
            total += amount
            lines.append({
                "product_id": product["id"], "name": product["name"], "price": product["price"],
                "quantity": row["quantity"], "amount": amount,
            })
        return {
            "cart_id": cart_id,
            "customer_id": customer_id,
            "state_version": version_rows[0]["state_version"] if version_rows else 0,
            "lines": lines,
            "total": total,
            "currency": "INR",
        }

    # ------------------------------------------------------------- mutations

    def add(self, customer_id: str, product_id: str, quantity: int = 1, *,
            expected_version: int | None = None) -> dict:
        products = self._products()
        product = products.get(product_id)
        if not product or product.get("options"):
            raise ValueError("Choose a purchasable product variant")
        quantity = max(1, quantity)
        cart_id = self.active_cart_id(customer_id)
        with self.store.transaction() as tx:
            self._guard_version(tx, cart_id, expected_version)
            rows = tx.rows(
                "SELECT quantity FROM cart_lines WHERE cart_id=? AND product_id=?", (cart_id, product_id)
            )
            current = rows[0]["quantity"] if rows else 0
            target = min(current + quantity, MAX_QUANTITY)
            self._reserve(tx, product_id, current, target)
            tx.execute(
                "INSERT INTO cart_lines (cart_id,product_id,quantity) VALUES (?,?,?) "
                "ON CONFLICT(cart_id,product_id) DO UPDATE SET quantity=?",
                (cart_id, product_id, target, target),
            )
            self._bump(tx, cart_id)
        return self._render(cart_id, customer_id)

    def set_quantity(self, customer_id: str, product_id: str, quantity: int, *,
                     expected_version: int | None = None) -> dict:
        if quantity <= 0:
            return self.remove(customer_id, product_id, expected_version=expected_version)
        target = min(quantity, MAX_QUANTITY)
        cart_id = self.active_cart_id(customer_id)
        with self.store.transaction() as tx:
            self._guard_version(tx, cart_id, expected_version)
            rows = tx.rows(
                "SELECT quantity FROM cart_lines WHERE cart_id=? AND product_id=?", (cart_id, product_id)
            )
            current = rows[0]["quantity"] if rows else 0
            self._reserve(tx, product_id, current, target)
            tx.execute(
                "INSERT INTO cart_lines (cart_id,product_id,quantity) VALUES (?,?,?) "
                "ON CONFLICT(cart_id,product_id) DO UPDATE SET quantity=?",
                (cart_id, product_id, target, target),
            )
            self._bump(tx, cart_id)
        return self._render(cart_id, customer_id)

    def remove(self, customer_id: str, product_id: str, *, expected_version: int | None = None) -> dict:
        cart_id = self.active_cart_id(customer_id)
        with self.store.transaction() as tx:
            self._guard_version(tx, cart_id, expected_version)
            rows = tx.rows(
                "SELECT quantity FROM cart_lines WHERE cart_id=? AND product_id=?", (cart_id, product_id)
            )
            current = rows[0]["quantity"] if rows else 0
            tx.execute("DELETE FROM cart_lines WHERE cart_id=? AND product_id=?", (cart_id, product_id))
            if current:
                tx.execute("UPDATE products SET stock=stock+? WHERE id=?", (current, product_id))
            self._bump(tx, cart_id)
        return self._render(cart_id, customer_id)

    def close(self, customer_id: str, status: str = "checked_out") -> None:
        """Retire the active cart so the next read opens a fresh one."""
        self.store.execute(
            "UPDATE customer_carts SET status=? WHERE customer_id=? AND status='active'",
            (status, customer_id),
        )

    # --------------------------------------------------------------- helpers

    @staticmethod
    def _guard_version(tx: Any, cart_id: str, expected_version: int | None) -> None:
        if expected_version is None:
            return
        rows = tx.rows("SELECT state_version FROM customer_carts WHERE id=?", (cart_id,))
        actual = rows[0]["state_version"] if rows else 0
        if actual != expected_version:
            raise ConflictError(
                f"Cart changed since you last read it (expected version {expected_version}, found {actual})"
            )

    @staticmethod
    def _bump(tx: Any, cart_id: str) -> None:
        tx.execute("UPDATE customer_carts SET state_version=state_version+1 WHERE id=?", (cart_id,))

    @staticmethod
    def _reserve(tx: Any, product_id: str, current: int, target: int) -> None:
        """Move the line from `current` to `target` units, holding the delta out of
        `products.stock` in the same transaction as the cart write, so two customers
        racing for the last units can't both pass a check against stock neither holds."""
        delta = target - current
        if delta > 0:
            updated = tx.execute(
                "UPDATE products SET stock=stock-? WHERE id=? AND stock>=?", (delta, product_id, delta)
            )
            if updated.rowcount == 0:
                raise ValueError("Requested quantity is unavailable")
        elif delta < 0:
            tx.execute("UPDATE products SET stock=stock+? WHERE id=?", (-delta, product_id))


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
