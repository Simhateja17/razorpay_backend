"""Staged merchant changes and host-controlled approval.

The split that matters: `stage` is the only verb the merchant agent can reach,
and it can only ever produce a `pending` row. `approve`/`reject` require an
authenticated operator, and `apply` re-validates policy against current state
before touching anything (ADR 0016). There is no method here that stages and
applies in one step, so no model-accessible path can apply a change.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from .evidence import Actor, Correlation, EvidenceLedger
from .state_machines import MERCHANT_CHANGE
from .store import Store
from .timeutil import now as _now

KINDS = ("inventory_action", "price_update", "promotion", "campaign", "listing_update")

# Bounds re-checked at application time, not just at staging time, because the
# world may have moved between the proposal and the operator's decision.
POLICY_BOUNDS = {
    "price_update": {"max_change_ratio": 0.25},
    "inventory_action": {"max_units": 500},
}


class PolicyViolation(Exception):
    """The change is outside what policy allows, at staging or at application."""


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:16]}"


class MerchantChangeRepository:
    def __init__(self, store: Store, ledger: EvidenceLedger) -> None:
        self.store, self.ledger = store, ledger

    # -------------------------------------------------- staging (agent-reachable)

    def stage(self, *, operator_id: str, kind: str, target_type: str, target_id: str | None,
              before: dict, after: dict, rationale: str,
              correlation: Correlation | None = None) -> dict:
        """Record a proposal. Always `pending`; never applies anything."""
        correlation = correlation or Correlation()
        actor = Actor("agent", operator_id, "merchant")
        if kind not in KINDS:
            self.ledger.record(
                actor=actor, action="stage_merchant_change", reason=f"Unknown change kind {kind!r}",
                outcome="blocked", correlation=correlation)
            raise ValueError(f"unknown change kind {kind!r}; expected one of {KINDS}")
        if not rationale.strip():
            raise ValueError("a staged change requires a rationale")
        try:
            self.check_policy(kind, before, after)
        except PolicyViolation as exc:
            self.ledger.record(
                actor=actor, action="stage_merchant_change", reason=str(exc), outcome="blocked",
                target_type=target_type, target_id=target_id,
                policy_checks={"kind": kind, "before": before, "after": after},
                correlation=correlation)
            raise

        change_id = _id("chg")
        with self.store.transaction() as tx:
            tx.execute(
                "INSERT INTO merchant_changes (id,operator_id,kind,target_type,target_id,"
                "before_doc,after_doc,rationale,status,created_at) VALUES (?,?,?,?,?,?,?,?,'pending',?)",
                (change_id, operator_id, kind, target_type, target_id, json.dumps(before),
                 json.dumps(after), rationale, _now()))
            self.ledger.record(
                actor=actor, action="stage_merchant_change", reason=rationale, outcome="applied",
                target_type=target_type, target_id=target_id,
                state_ref={"change_id": change_id, "before": before, "after": after},
                correlation=correlation, tx=tx)
        return self.read(change_id)

    def read(self, change_id: str) -> dict:
        rows = self.store.rows("SELECT * FROM merchant_changes WHERE id=?", (change_id,))
        if not rows:
            raise ValueError(f"unknown merchant change {change_id!r}")
        change = rows[0]
        change["before"] = json.loads(change.pop("before_doc"))
        change["after"] = json.loads(change.pop("after_doc"))
        return change

    def pending(self, limit: int = 50) -> list[dict]:
        return [self.read(row["id"]) for row in self.store.rows(
            "SELECT id FROM merchant_changes WHERE status='pending' ORDER BY created_at LIMIT ?", (limit,))]

    # ------------------------------------------- decision (operator-only)

    def decide(self, *, change_id: str, operator_id: str, decision: str, note: str | None = None,
               correlation: Correlation | None = None) -> dict:
        if decision not in {"approved", "rejected"}:
            raise ValueError("a decision is either 'approved' or 'rejected'")
        change = self.read(change_id)
        MERCHANT_CHANGE.check(change["status"], decision)
        correlation = correlation or Correlation()
        with self.store.transaction() as tx:
            tx.execute(
                "UPDATE merchant_changes SET status=?, decided_at=? WHERE id=? AND status='pending'",
                (decision, _now(), change_id))
            tx.execute(
                "INSERT INTO merchant_approvals (id,change_id,operator_id,decision,note,policy_checks,decided_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (_id("apr"), change_id, operator_id, decision, note,
                 json.dumps({"bounds": POLICY_BOUNDS.get(change["kind"], {})}), _now()))
            self.ledger.record(
                actor=Actor("merchant_operator", operator_id, "merchant"),
                action=f"{decision}_merchant_change",
                reason=note or f"Operator {decision} the staged change", outcome="applied",
                target_type="merchant_change", target_id=change_id,
                state_ref={"kind": change["kind"], "after": change["after"]},
                correlation=correlation, tx=tx)
        return self.read(change_id)

    def apply(self, *, change_id: str, operator_id: str, applier: Callable[[dict, Any], None],
              correlation: Correlation | None = None) -> dict:
        """Apply an approved change, re-validating policy against current state first."""
        change = self.read(change_id)
        MERCHANT_CHANGE.check(change["status"], "applied")
        correlation = correlation or Correlation()
        actor = Actor("merchant_operator", operator_id, "merchant")
        try:
            self.check_policy(change["kind"], change["before"], change["after"])
        except PolicyViolation as exc:
            with self.store.transaction() as tx:
                tx.execute(
                    "UPDATE merchant_changes SET status='failed' WHERE id=? AND status='approved'",
                    (change_id,))
                self.ledger.record(
                    actor=actor, action="apply_merchant_change",
                    reason=f"Revalidation failed at application time: {exc}", outcome="blocked",
                    target_type="merchant_change", target_id=change_id, correlation=correlation, tx=tx)
            raise

        with self.store.transaction() as tx:
            applier(change, tx)
            tx.execute(
                "UPDATE merchant_changes SET status='applied', applied_at=? WHERE id=? AND status='approved'",
                (_now(), change_id))
            self.ledger.record(
                actor=actor, action="apply_merchant_change", reason=change["rationale"],
                outcome="applied", target_type=change["target_type"], target_id=change["target_id"],
                state_ref={"change_id": change_id, "after": change["after"]},
                correlation=correlation, tx=tx)
        return self.read(change_id)

    # ------------------------------------------------------------- policy

    @staticmethod
    def check_policy(kind: str, before: dict, after: dict) -> None:
        """Deterministic bounds. The same function runs at staging and at application."""
        if kind == "price_update":
            old, new = before.get("amount_minor"), after.get("amount_minor")
            if not isinstance(old, int) or not isinstance(new, int) or old <= 0 or new <= 0:
                raise PolicyViolation("a price update needs positive integer amount_minor in both documents")
            ratio = abs(new - old) / old
            limit = POLICY_BOUNDS["price_update"]["max_change_ratio"]
            if ratio > limit:
                raise PolicyViolation(
                    f"price change of {ratio:.0%} exceeds the {limit:.0%} bound")
        elif kind == "inventory_action":
            units = after.get("units")
            if not isinstance(units, int) or units == 0:
                raise PolicyViolation("an inventory action needs a non-zero integer unit count")
            if abs(units) > POLICY_BOUNDS["inventory_action"]["max_units"]:
                raise PolicyViolation(
                    f"{units} units exceeds the {POLICY_BOUNDS['inventory_action']['max_units']} unit bound")
