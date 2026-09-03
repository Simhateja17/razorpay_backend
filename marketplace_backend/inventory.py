"""Inventory: sellable stock, movements, and reservations.

Two rules hold the whole thing together:

  * `sellable = on_hand - reserved` is never stored, so it cannot drift from the
    numbers it is derived from.
  * Stock is reserved when a checkout is confirmed, never when an item is added to
    a cart (ADR 0012). A cart is an intention; a confirmed checkout is a claim.

Every change to `on_hand` writes an `inventory_movements` row in the same
transaction, so the ledger and the level always reconcile.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from .state_machines import RESERVATION
from .store import Store
from .timeutil import now as _now

RESERVATION_MINUTES = 15


class InsufficientStock(Exception):
    """Not enough sellable stock at any location to satisfy the request."""


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:16]}"


class InventoryRepository:
    def __init__(self, store: Store) -> None:
        self.store = store

    # ------------------------------------------------------------------ reads

    def sellable(self, variant_id: str, location_id: str | None = None) -> int:
        sql = "SELECT on_hand,reserved FROM inventory_levels WHERE variant_id=?"
        params: tuple[Any, ...] = (variant_id,)
        if location_id:
            sql += " AND location_id=?"
            params += (location_id,)
        return sum(row["on_hand"] - row["reserved"] for row in self.store.rows(sql, params))

    def levels(self, variant_id: str) -> list[dict]:
        return self.store.rows(
            "SELECT variant_id,location_id,on_hand,reserved FROM inventory_levels "
            "WHERE variant_id=? ORDER BY location_id", (variant_id,))

    # ----------------------------------------------------------- adjustments

    def receive(self, variant_id: str, location_id: str, quantity: int, *,
                reason: str = "receipt", reference_type: str | None = None,
                reference_id: str | None = None) -> None:
        """Add physical stock, explained by exactly one movement."""
        if quantity <= 0:
            raise ValueError("receiving requires a positive quantity")
        with self.store.transaction() as tx:
            self._adjust(tx, variant_id, location_id, quantity, reason, reference_type, reference_id)

    def _adjust(self, tx: Any, variant_id: str, location_id: str, delta: int, reason: str,
                reference_type: str | None, reference_id: str | None) -> None:
        rows = tx.rows(
            "SELECT on_hand FROM inventory_levels WHERE variant_id=? AND location_id=?",
            (variant_id, location_id))
        if rows:
            tx.execute(
                "UPDATE inventory_levels SET on_hand=on_hand+?, updated_at=? "
                "WHERE variant_id=? AND location_id=?", (delta, _now(), variant_id, location_id))
        else:
            tx.execute(
                "INSERT INTO inventory_levels (variant_id,location_id,on_hand,reserved,updated_at) "
                "VALUES (?,?,?,0,?)", (variant_id, location_id, delta, _now()))
        tx.execute(
            "INSERT INTO inventory_movements (id,variant_id,location_id,delta,reason,"
            "reference_type,reference_id,created_at) VALUES (?,?,?,?,?,?,?,?)",
            (_id("mv"), variant_id, location_id, delta, reason, reference_type, reference_id, _now()))

    # ---------------------------------------------------------- reservations

    def reserve(self, *, order_id: str, variant_id: str, quantity: int,
                location_id: str | None = None, minutes: int = RESERVATION_MINUTES,
                tx: Any = None) -> dict:
        """Hold `quantity` units for an order, or refuse.

        The conditional UPDATE is the whole concurrency story: two confirmations
        racing for the last unit both compute the same sellable figure, but only
        one of them changes a row, and the loser raises instead of overselling.
        """
        if quantity <= 0:
            raise ValueError("reserving requires a positive quantity")

        def run(handle: Any) -> dict:
            candidates = handle.rows(
                "SELECT location_id,on_hand,reserved FROM inventory_levels "
                "WHERE variant_id=? ORDER BY location_id", (variant_id,))
            if location_id:
                candidates = [row for row in candidates if row["location_id"] == location_id]
            for row in candidates:
                if row["on_hand"] - row["reserved"] < quantity:
                    continue
                updated = handle.execute(
                    "UPDATE inventory_levels SET reserved=reserved+?, updated_at=? "
                    "WHERE variant_id=? AND location_id=? AND on_hand-reserved>=?",
                    (quantity, _now(), variant_id, row["location_id"], quantity))
                if updated.rowcount == 0:
                    continue  # lost the race for these units; try the next location
                reservation_id = _id("res")
                expires = (datetime.now(UTC) + timedelta(minutes=minutes)).isoformat()
                handle.execute(
                    "INSERT INTO inventory_reservations (id,order_id,variant_id,location_id,"
                    "quantity,status,expires_at,created_at) VALUES (?,?,?,?,?,'held',?,?)",
                    (reservation_id, order_id, variant_id, row["location_id"], quantity, expires, _now()))
                return {"id": reservation_id, "variant_id": variant_id,
                        "location_id": row["location_id"], "quantity": quantity,
                        "status": "held", "expires_at": expires}
            raise InsufficientStock(
                f"only {self.sellable(variant_id)} of {variant_id} is sellable; {quantity} requested")

        if tx is not None:
            return run(tx)
        with self.store.transaction() as own:
            return run(own)

    def consume(self, reservation_id: str, *, tx: Any = None) -> None:
        """A verified payment turns the hold into a sale: reserved and on_hand both fall."""
        self._resolve(reservation_id, "consumed", release_stock=False, reason="sale", tx=tx)

    def release(self, reservation_id: str, *, tx: Any = None) -> None:
        """Cancellation returns the units to sellable stock."""
        self._resolve(reservation_id, "released", release_stock=True, reason="reservation_release", tx=tx)

    def expire_due(self, *, now: str | None = None) -> list[str]:
        """Release every hold whose window has passed. Safe to run repeatedly."""
        cutoff = now or _now()
        due = self.store.rows(
            "SELECT id FROM inventory_reservations WHERE status='held' AND expires_at<=?", (cutoff,))
        for row in due:
            self._resolve(row["id"], "expired", release_stock=True, reason="reservation_release")
        return [row["id"] for row in due]

    def _resolve(self, reservation_id: str, target: str, *, release_stock: bool, reason: str,
                 tx: Any = None) -> None:
        def run(handle: Any) -> None:
            rows = handle.rows(
                "SELECT variant_id,location_id,quantity,status FROM inventory_reservations WHERE id=?",
                (reservation_id,))
            if not rows:
                raise ValueError(f"unknown reservation {reservation_id!r}")
            reservation = rows[0]
            RESERVATION.check(reservation["status"], target)
            handle.execute(
                "UPDATE inventory_reservations SET status=?, resolved_at=? WHERE id=? AND status='held'",
                (target, _now(), reservation_id))
            handle.execute(
                "UPDATE inventory_levels SET reserved=reserved-?, updated_at=? "
                "WHERE variant_id=? AND location_id=?",
                (reservation["quantity"], _now(), reservation["variant_id"], reservation["location_id"]))
            if not release_stock:
                # Consumed units leave the building: on_hand falls too, with a movement.
                self._adjust(handle, reservation["variant_id"], reservation["location_id"],
                             -reservation["quantity"], reason, "reservation", reservation_id)

        if tx is not None:
            run(tx)
            return
        with self.store.transaction() as own:
            run(own)

    # ------------------------------------------------------------ invariants

    def reconcile(self, variant_id: str) -> dict:
        """`on_hand` must equal the sum of movements, and `reserved` the sum of live holds."""
        levels = self.levels(variant_id)
        movements = self.store.rows(
            "SELECT location_id,SUM(delta) AS total FROM inventory_movements "
            "WHERE variant_id=? GROUP BY location_id", (variant_id,))
        held = self.store.rows(
            "SELECT location_id,SUM(quantity) AS total FROM inventory_reservations "
            "WHERE variant_id=? AND status='held' GROUP BY location_id", (variant_id,))
        movement_by_location = {row["location_id"]: int(row["total"] or 0) for row in movements}
        held_by_location = {row["location_id"]: int(row["total"] or 0) for row in held}
        problems = []
        for level in levels:
            location = level["location_id"]
            if level["on_hand"] != movement_by_location.get(location, 0):
                problems.append(
                    f"{variant_id}@{location}: on_hand {level['on_hand']} != movements "
                    f"{movement_by_location.get(location, 0)}")
            if level["reserved"] != held_by_location.get(location, 0):
                problems.append(
                    f"{variant_id}@{location}: reserved {level['reserved']} != held "
                    f"{held_by_location.get(location, 0)}")
        return {"variant_id": variant_id, "balanced": not problems, "problems": problems}
