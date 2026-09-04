"""Payment recovery: what is stuck, and what a host can do about it (ADR 0030).

Two states are reachable today and were invisible until this module existed.

  **Dead-lettered outbox messages.** `Outbox.failed` parks a message after
  `max_attempts`. The order behind it is real, its stock is really held, and the
  customer is waiting for a payment link that will never be requested again. That
  is recoverable: the message goes back to `pending` and the host drains it.

  **Quarantined provider events.** `Inbox.mark` refuses a callback that does not
  match the order it names and records why. That is *not* recoverable, and this
  module does not pretend otherwise: a quarantined event stays quarantined. What a
  host can do is see it, see the order it claimed, and act on the order — retry the
  payment, or cancel it and release the stock. Re-applying a payload that failed
  verification is the one thing that would produce a wrong `paid`, which is the
  worst failure this system can make (ADR 0013).

Every control here is host-triggered and behind the operations token, exactly like
`/admin/expire` and `/admin/payments/drain`. None of it is in any tool list, and
no model-reachable path reaches this module (ADR 0005).
"""

from __future__ import annotations

import json
from typing import Any

from .checkout import CheckoutRepository
from .evidence import Actor, Correlation, EvidenceLedger
from .state_machines import OUTBOX, TransitionError
from .store import Store
from .timeutil import now as _now


class RecoveryRefused(Exception):
    """A recovery action the rules do not allow, with the reason a human needs."""


class RecoveryService:
    def __init__(self, store: Store, checkout: CheckoutRepository,
                 ledger: EvidenceLedger) -> None:
        self.store, self.checkout, self.ledger = store, checkout, ledger

    # -- what is stuck ---------------------------------------------------------

    def queue(self, limit: int = 50) -> dict:
        """Everything currently needing a human, with the reason attached to each."""
        limit = max(1, min(limit, 200))
        return {
            "dead_letters": self.dead_letters(limit),
            "quarantined": self.quarantined(limit),
            "unprocessed": self.unprocessed(limit),
            "stuck_orders": self.stuck_orders(limit),
        }

    def dead_letters(self, limit: int = 50) -> list[dict]:
        """Outbox messages that exhausted their attempts. Each one is an external
        effect that was scheduled, committed, and never delivered."""
        rows = self.store.rows(
            "SELECT * FROM outbox_messages WHERE status='dead_letter' "
            "ORDER BY created_at DESC LIMIT ?", (max(1, min(limit, 200)),))
        out = []
        for row in rows:
            payload = _load(row["payload"])
            order_id = payload.get("order_id") if isinstance(payload, dict) else None
            out.append({
                "message_id": row["id"], "topic": row["topic"], "attempts": row["attempts"],
                "last_error": row["last_error"], "correlation_id": row["correlation_id"],
                "created_at": _text(row["created_at"]), "payload": payload,
                "order_id": order_id,
                "order": self._order_summary(order_id),
                "recovery_actions": ["retry_message"],
            })
        return out

    def quarantined(self, limit: int = 50) -> list[dict]:
        """Provider events refused by verification, with the order each one claimed."""
        rows = self.store.rows(
            "SELECT * FROM inbox_events WHERE status='quarantined' "
            "ORDER BY received_at DESC LIMIT ?", (max(1, min(limit, 200)),))
        return [self._event(row, ["acknowledge"]) for row in rows]

    def unprocessed(self, limit: int = 50) -> list[dict]:
        """Events stored but never decided — a delivery interrupted mid-flight. These
        can genuinely be re-run, because they have not been applied or refused yet."""
        rows = self.store.rows(
            "SELECT * FROM inbox_events WHERE status='received' "
            "ORDER BY received_at DESC LIMIT ?", (max(1, min(limit, 200)),))
        return [self._event(row, ["reprocess_event"]) for row in rows]

    def stuck_orders(self, limit: int = 50) -> list[dict]:
        """Orders holding stock with nothing in flight to resolve them: no live
        attempt, and not yet paid. The order view shows the same actions (ADR 0030)."""
        rows = self.store.rows(
            "SELECT * FROM commerce_orders WHERE status IN "
            "('pending_payment','payment_verification_pending') "
            "ORDER BY created_at DESC LIMIT ?", (max(1, min(limit, 200)),))
        out = []
        for row in rows:
            live = self.store.rows(
                "SELECT COUNT(*) AS n FROM payment_attempts WHERE order_id=? "
                "AND status IN ('created','pending')", (row["id"],))[0]["n"]
            if int(live):
                continue
            out.append({**self._order_summary(row["id"]),
                        "recovery_actions": order_recovery_actions(row["status"])})
        return out

    def _event(self, row: dict, actions: list[str]) -> dict:
        payload = _load(row["payload"])
        return {
            "inbox_id": row["id"], "provider": row["provider"],
            "provider_event_id": row["provider_event_id"], "event_type": row["event_type"],
            "status": row["status"], "quarantine_reason": row["quarantine_reason"],
            "correlation_id": row.get("correlation_id"),
            "received_at": _text(row["received_at"]),
            "processed_at": _text(row["processed_at"]),
            "payload": payload,
            "order": self._order_for_event(payload),
            "recovery_actions": actions,
        }

    def _order_for_event(self, payload: Any) -> dict | None:
        """The order a provider event claims, found the way the processor finds it:
        through the attempt that holds the provider reference."""
        if not isinstance(payload, dict):
            return None
        reference = _reference(payload)
        if not reference:
            return None
        rows = self.store.rows(
            "SELECT order_id FROM payment_attempts WHERE provider_reference=? "
            "ORDER BY created_at DESC LIMIT 1", (reference,))
        return self._order_summary(rows[0]["order_id"]) if rows else None

    def _order_summary(self, order_id: str | None) -> dict | None:
        if not order_id:
            return None
        rows = self.store.rows(
            "SELECT id,customer_id,status,total_minor,amount_paid_minor,origin,correlation_id,"
            "created_at FROM commerce_orders WHERE id=?", (order_id,))
        if not rows:
            return None
        order = rows[0]
        return {"order_id": order["id"], "customer_id": order["customer_id"],
                "status": order["status"], "total_minor": order["total_minor"],
                "amount_paid_minor": order["amount_paid_minor"], "origin": order["origin"],
                "correlation_id": order["correlation_id"],
                "created_at": _text(order["created_at"])}

    # -- the controls ----------------------------------------------------------

    def retry_message(self, message_id: str, *, operator: str = "host") -> dict:
        """Return a dead-lettered message to the queue.

        Safe to repeat: the effect it schedules is itself idempotent per attempt, so
        a redelivered payment-link request recovers the existing link rather than
        creating a second one.
        """
        rows = self.store.rows("SELECT * FROM outbox_messages WHERE id=?", (message_id,))
        if not rows:
            raise RecoveryRefused(f"unknown outbox message {message_id!r}")
        message = rows[0]
        if message["status"] != "dead_letter":
            raise RecoveryRefused(
                f"that message is {message['status']}, not dead_letter; nothing was changed")
        # `dead_letter -> pending` has to be a legal edge, not a raw UPDATE past the
        # state machine, or the recovery control would be the one path that does not
        # obey the model everything else is checked against.
        OUTBOX.check(message["status"], "pending")
        self.store.execute(
            "UPDATE outbox_messages SET status='pending', available_at=?, last_error=? "
            "WHERE id=? AND status='dead_letter'",
            (_now(), f"requeued by {operator} after dead-letter", message_id))
        self.ledger.record(
            actor=Actor("system", operator, "shopping"), action="retry_dead_letter",
            reason="Host requeued a dead-lettered external effect", outcome="applied",
            target_type="outbox_message", target_id=message_id,
            state_ref={"topic": message["topic"], "attempts": message["attempts"],
                       "last_error": message["last_error"]},
            correlation=Correlation(
                correlation_id=message["correlation_id"] or Correlation().correlation_id))
        return {"message_id": message_id, "status": "pending"}

    def acknowledge(self, inbox_id: str, *, note: str, operator: str = "host") -> dict:
        """Record that a human has read a quarantined event and what they concluded.

        This deliberately does not change the event's status. A quarantined payload
        failed verification; the record of that refusal is the evidence, and moving
        it would erase the thing the audit view exists to show. The recovery is on
        the *order* — retry it, or cancel it and release the stock.
        """
        rows = self.store.rows("SELECT * FROM inbox_events WHERE id=?", (inbox_id,))
        if not rows:
            raise RecoveryRefused(f"unknown inbox event {inbox_id!r}")
        event = rows[0]
        if event["status"] != "quarantined":
            raise RecoveryRefused(
                f"that event is {event['status']}, not quarantined; nothing was changed")
        if not note.strip():
            raise RecoveryRefused("acknowledging a quarantined event requires a note")
        self.ledger.record(
            actor=Actor("system", operator, "shopping"), action="acknowledge_quarantine",
            reason=note.strip(), outcome="applied", target_type="inbox_event",
            target_id=inbox_id,
            state_ref={"provider_event_id": event["provider_event_id"],
                       "quarantine_reason": event["quarantine_reason"]},
            policy_checks={"status_unchanged": "quarantined",
                           "why": "a payload that failed verification is never re-applied"},
            data_origin="razorpay_test",
            correlation=Correlation(
                correlation_id=event.get("correlation_id") or Correlation().correlation_id))
        return {"inbox_id": inbox_id, "status": "quarantined", "acknowledged": True}

    def reprocess_event(self, inbox_id: str, processor: Any, *,
                        operator: str = "host") -> dict:
        """Re-run a stored-but-undecided event through the ordinary processor.

        The event is not applied here: it is handed back to `WebhookProcessor`, which
        applies exactly the same verification it would for a live delivery. A payload
        that does not match is quarantined now rather than silently forgotten.
        """
        rows = self.store.rows("SELECT * FROM inbox_events WHERE id=?", (inbox_id,))
        if not rows:
            raise RecoveryRefused(f"unknown inbox event {inbox_id!r}")
        event = rows[0]
        if event["status"] != "received":
            raise RecoveryRefused(
                f"that event is {event['status']}; only an undecided event can be reprocessed")
        payload = _load(event["payload"])
        if not isinstance(payload, dict):
            raise RecoveryRefused("that event's payload is not a readable object")
        # The stored row already holds this provider event id, so re-entering
        # `process` would see its own row and report a duplicate. The row is deleted
        # and the payload replayed, which is the only way to run the real verification
        # rather than a copy of it that could drift.
        self.store.execute("DELETE FROM inbox_events WHERE id=? AND status='received'", (inbox_id,))
        try:
            outcome = processor.process({**payload, "id": event["provider_event_id"],
                                         "event": event["event_type"]})
        except Exception as exc:  # noqa: BLE001 — the row must not be lost on a failure
            self.store.execute(
                "INSERT INTO inbox_events (id,provider,provider_event_id,event_type,payload,"
                "status,correlation_id,received_at) VALUES (?,?,?,?,?,'received',?,?)",
                (event["id"], event["provider"], event["provider_event_id"],
                 event["event_type"], event["payload"], event.get("correlation_id"),
                 event["received_at"]))
            raise RecoveryRefused(f"reprocessing failed and the event was left as it was: {exc}") from exc
        self.ledger.record(
            actor=Actor("system", operator, "shopping"), action="reprocess_provider_event",
            reason="Host replayed a stored provider event through the ordinary verification",
            outcome="applied" if outcome.get("result") == "applied" else "blocked",
            target_type="inbox_event", target_id=outcome.get("inbox_id") or inbox_id,
            state_ref=outcome, data_origin="razorpay_test",
            correlation=Correlation(
                correlation_id=event.get("correlation_id") or Correlation().correlation_id))
        return outcome

    def cancel_order(self, order_id: str, *, reason: str, operator: str = "host") -> dict:
        """Give up on an order and release what it holds. The other half of ADR 0030:
        expiry does this on a timer, and this is the same act on a human's judgement."""
        if not reason.strip():
            raise RecoveryRefused("cancelling an order requires a reason")
        try:
            order = self.checkout.read_order(order_id)
        except ValueError as exc:
            raise RecoveryRefused(f"unknown order {order_id!r}") from exc
        try:
            cancelled = self.checkout.cancel(
                order_id, reason=f"{reason.strip()} (cancelled by {operator})",
                correlation=Correlation(
                    correlation_id=order.get("correlation_id") or Correlation().correlation_id,
                    demo_run_id=order.get("demo_run_id")))
        except TransitionError as exc:
            raise RecoveryRefused(
                f"that order is {order['status']} and cannot be cancelled: {exc}") from exc
        return {"order_id": order_id, "status": cancelled["status"]}


def order_recovery_actions(status: str) -> list[str]:
    """What can still be done to an order in this state, named the same way in the
    order view and in the recovery queue so the two never disagree."""
    if status == "pending_payment":
        return ["retry_payment", "cancel_order"]
    if status == "payment_verification_pending":
        # Not cancellable: a verified event may be in flight, and cancelling here
        # could void an order the provider is about to report as paid.
        return ["await_verification"]
    return []


def _reference(payload: dict) -> str | None:
    body = payload.get("payload") or {}
    for key in ("payment_link", "payment", "order"):
        entity = (body.get(key) or {}).get("entity")
        if isinstance(entity, dict) and entity.get("id"):
            return str(entity["id"])
    return None


def _load(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except ValueError:
        return value


def _text(value: Any) -> str | None:
    if value is None:
        return None
    return value if isinstance(value, str) else value.isoformat()


__all__ = ["RecoveryRefused", "RecoveryService", "order_recovery_actions"]
