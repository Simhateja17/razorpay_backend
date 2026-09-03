"""Typed tool outcomes.

The architecture's tool boundary ends: "Expected business refusals return typed
applied, blocked, unavailable, failed, or conflict outcomes rather than becoming
unstructured exceptions." These five are also the CHECK constraint on
`tool_executions.outcome` and on `evidence_records.outcome`, so the value a tool
produces is the value the ledger stores — one vocabulary, not a translation.
"""

from __future__ import annotations

from enum import StrEnum

from commerce_common.streaming import ToolOutcome


class Outcome(StrEnum):
    APPLIED = "applied"
    BLOCKED = "blocked"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"
    CONFLICT = "conflict"


class BusinessRefusal(Exception):
    """A refusal the domain expects: the executor turns it into a typed outcome the
    model can recover from, never into an infrastructure error (ADR 0015)."""

    outcome = Outcome.BLOCKED

    def __init__(self, message: str, *, gate: str | None = None) -> None:
        super().__init__(message)
        self.gate = gate or self.__class__.__name__


class Unavailable(BusinessRefusal):
    """The store has no such thing for this item or context — not an outage."""

    outcome = Outcome.UNAVAILABLE


class Conflict(BusinessRefusal):
    """The caller's expected authoritative-state version is stale (ADR 0029). The
    message carries the fresh state so the model can re-read and retry deliberately."""

    outcome = Outcome.CONFLICT


# `ToolOutcome` is the reference runtime's transport and carries only three states
# (ok, error, blocked). Cartisan needs five, so the extra two ride on the outcome as
# an attribute rather than in a parallel object the turn loop would have to thread.
_TYPED = "_cartisan_outcome"


def tag(outcome: ToolOutcome, kind: Outcome) -> ToolOutcome:
    setattr(outcome, _TYPED, kind)
    return outcome


def refusal(error: BusinessRefusal) -> ToolOutcome:
    """The `ToolOutcome` for an expected business refusal, typed for the ledger."""
    if error.outcome is Outcome.BLOCKED:
        return tag(ToolOutcome.held(error.gate, str(error)), Outcome.BLOCKED)
    return tag(ToolOutcome.error(str(error)), error.outcome)


def classify(outcome: ToolOutcome) -> Outcome:
    """The typed outcome for one finished call: the tag when a refusal set one, else
    read off the transport — held is blocked, an error is failed, anything else applied."""
    tagged = getattr(outcome, _TYPED, None)
    if isinstance(tagged, Outcome):
        return tagged
    if outcome.blocked is not None:
        return Outcome.BLOCKED
    if outcome.is_error:
        return Outcome.FAILED
    return Outcome.APPLIED
