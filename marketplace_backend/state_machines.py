"""Allowed transitions for every consequential commerce entity.

Each machine is a plain table of `state -> allowed next states`. Keeping them
declarative means an illegal transition is a typed refusal at one chokepoint
rather than an ad-hoc `if` somewhere in a request handler, and the same table is
what the tests assert against.

Terminal states have no outgoing transitions: once an order is `paid` it cannot
walk back to `pending_payment`, and once a reservation is `released` it cannot be
re-held. Recovery from a terminal state creates a new entity instead.
"""

from __future__ import annotations

from dataclasses import dataclass


class TransitionError(Exception):
    """A state change that the machine does not permit."""


@dataclass(frozen=True)
class StateMachine:
    name: str
    initial: str
    transitions: dict[str, frozenset[str]]

    @property
    def states(self) -> frozenset[str]:
        return frozenset(self.transitions)

    @property
    def terminal_states(self) -> frozenset[str]:
        return frozenset(state for state, nexts in self.transitions.items() if not nexts)

    def allows(self, current: str, target: str) -> bool:
        return target in self.transitions.get(current, frozenset())

    def check(self, current: str, target: str) -> None:
        """Raise unless `current -> target` is permitted."""
        if current not in self.transitions:
            raise TransitionError(f"{self.name}: unknown state {current!r}")
        if target not in self.transitions:
            raise TransitionError(f"{self.name}: unknown state {target!r}")
        if not self.allows(current, target):
            allowed = ", ".join(sorted(self.transitions[current])) or "nothing (terminal)"
            raise TransitionError(
                f"{self.name}: cannot move from {current!r} to {target!r}; allowed: {allowed}"
            )


def _machine(name: str, initial: str, transitions: dict[str, set[str]]) -> StateMachine:
    return StateMachine(name, initial, {state: frozenset(nexts) for state, nexts in transitions.items()})


# A staged checkout is an immutable preview. It is confirmed once, or it expires,
# or a newer staging supersedes it. It never returns to `staged`.
CHECKOUT_STAGE = _machine("checkout_stage", "staged", {
    "staged": {"confirmed", "expired", "superseded"},
    "confirmed": set(),
    "expired": set(),
    "superseded": set(),
})

# Stock is held on checkout confirmation. It is consumed by a verified payment,
# released by cancellation, or expired by the sweeper — each exactly once.
RESERVATION = _machine("reservation", "held", {
    "held": {"consumed", "released", "expired"},
    "consumed": set(),
    "released": set(),
    "expired": set(),
})

# An order becomes `paid` only from a verified provider outcome. A redirect only
# reaches `payment_verification_pending`, which is not a paid state (ADR 0013).
ORDER = _machine("order", "pending_payment", {
    "pending_payment": {"payment_verification_pending", "paid", "cancelled", "expired"},
    "payment_verification_pending": {"paid", "pending_payment", "cancelled", "expired"},
    "paid": {"refunded"},
    "cancelled": set(),
    "expired": set(),
    "refunded": set(),
})

# One order may have many attempts; a failed attempt is terminal for that attempt
# and the customer retries with a new one.
PAYMENT_ATTEMPT = _machine("payment_attempt", "created", {
    "created": {"pending", "succeeded", "failed", "cancelled", "expired"},
    "pending": {"succeeded", "failed", "cancelled", "expired"},
    "succeeded": set(),
    "failed": set(),
    "cancelled": set(),
    "expired": set(),
})

# The merchant agent may only create `pending`. Approval and application are
# host-controlled, and no model-accessible path reaches `applied` (ADR 0016).
MERCHANT_CHANGE = _machine("merchant_change", "pending", {
    "pending": {"approved", "rejected", "superseded"},
    "approved": {"applied", "failed"},
    "rejected": set(),
    "applied": set(),
    "failed": set(),
    "superseded": set(),
})

FULFILLMENT = _machine("fulfillment", "pending", {
    "pending": {"packed", "cancelled"},
    "packed": {"shipped", "cancelled"},
    "shipped": {"delivered"},
    "delivered": set(),
    "cancelled": set(),
})

REFUND = _machine("refund", "requested", {
    "requested": {"processing", "failed"},
    "processing": {"completed", "failed"},
    "completed": set(),
    "failed": set(),
})

TURN = _machine("turn", "received", {
    "received": {"running", "abandoned"},
    "running": {"awaiting_tool", "completed", "failed", "abandoned"},
    "awaiting_tool": {"running", "failed", "abandoned"},
    "completed": set(),
    "failed": set(),
    "abandoned": set(),
})

OUTBOX = _machine("outbox_message", "pending", {
    "pending": {"in_flight"},
    "in_flight": {"delivered", "failed"},
    # A failed delivery is retried by returning it to `pending`, until it is
    # parked in `dead_letter` for a human.
    "failed": {"pending", "dead_letter"},
    "delivered": set(),
    "dead_letter": set(),
})

INBOX = _machine("inbox_event", "received", {
    "received": {"processed", "ignored", "quarantined"},
    "processed": set(),
    "ignored": set(),
    "quarantined": set(),
})

ALL_MACHINES: tuple[StateMachine, ...] = (
    CHECKOUT_STAGE, RESERVATION, ORDER, PAYMENT_ATTEMPT, MERCHANT_CHANGE,
    FULFILLMENT, REFUND, TURN, OUTBOX, INBOX,
)
