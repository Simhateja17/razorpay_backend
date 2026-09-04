"""Phase 2 acceptance: the commerce core.

The acceptance criterion is that domain and transaction tests prove totals,
ownership, stock, deduplication, and allowed transitions.
"""

import json

import pytest

from marketplace_backend.checkout import (
    CheckoutRepository,
    PaymentVerificationMismatch,
    StageExpired,
    StageMismatch,
)
from marketplace_backend.evidence import (
    Actor,
    CommerceEventLog,
    Correlation,
    EvidenceLedger,
    Inbox,
    Outbox,
)
from marketplace_backend.inventory import InsufficientStock, InventoryRepository
from marketplace_backend.merchant_changes import MerchantChangeRepository, PolicyViolation
from marketplace_backend.state_machines import ALL_MACHINES, ORDER, RESERVATION, TransitionError
from marketplace_backend.store import Store

ALICE = "11111111-1111-1111-1111-111111111111"
BOB = "22222222-2222-2222-2222-222222222222"
OPERATOR = "33333333-3333-3333-3333-333333333333"


@pytest.fixture
def core(tmp_path):
    store = Store(tmp_path / "core.db")
    store.execute("INSERT INTO catalog_categories (id,name) VALUES ('cat-audio','Audio')")
    store.execute(
        "INSERT INTO catalog_products (id,sku_root,title,brand,category_id,description) "
        "VALUES ('prd-1','ASTER-EB','Aster Earbuds','Aster','cat-audio','Wireless earbuds')")
    for variant, sku in (("var-1", "ASTER-EB-BLK"), ("var-2", "ASTER-EB-WHT")):
        store.execute(
            "INSERT INTO catalog_variants (id,product_id,sku,title) VALUES (?,'prd-1',?,?)",
            (variant, sku, f"Aster Earbuds {sku[-3:]}"))
    store.execute(
        "INSERT INTO inventory_locations (id,code,name,region) VALUES ('loc-blr','BLR','Bengaluru','South')")
    store.execute(
        "INSERT INTO inventory_locations (id,code,name,region) VALUES ('loc-del','DEL','Delhi','North')")

    ledger = EvidenceLedger(store)
    inventory = InventoryRepository(store)
    outbox = Outbox(store)
    events = CommerceEventLog(store)
    checkout = CheckoutRepository(store, inventory, ledger, outbox, events)
    return {
        "store": store, "ledger": ledger, "inventory": inventory, "outbox": outbox,
        "events": events, "checkout": checkout, "inbox": Inbox(store),
        "changes": MerchantChangeRepository(store, ledger),
    }


def stage_for(core, customer_id=ALICE, quantity=1, unit_price=249900, version=1, **kwargs):
    return core["checkout"].stage(
        customer_id=customer_id, cart_id="cart-1", cart_state_version=version,
        lines=[{"variant_id": "var-1", "quantity": quantity, "unit_price_minor": unit_price}],
        fulfillment_option="standard", **kwargs)


# ============================================================== transitions


@pytest.mark.parametrize("machine", ALL_MACHINES, ids=lambda m: m.name)
def test_every_machine_is_internally_consistent(machine):
    """Every target state is itself a declared state, and the initial state exists."""
    assert machine.initial in machine.states
    for state, nexts in machine.transitions.items():
        assert state not in nexts, f"{machine.name}: {state} loops to itself"
        for target in nexts:
            assert target in machine.states, f"{machine.name}: {state} -> unknown {target}"


def test_terminal_states_are_terminal():
    assert ORDER.terminal_states == {"cancelled", "expired", "refunded"}
    assert RESERVATION.terminal_states == {"consumed", "released", "expired"}


def test_illegal_transitions_are_refused():
    ORDER.check("pending_payment", "paid")  # legal
    with pytest.raises(TransitionError, match="cannot move"):
        ORDER.check("paid", "pending_payment")
    with pytest.raises(TransitionError, match="cannot move"):
        ORDER.check("cancelled", "paid")
    with pytest.raises(TransitionError, match="unknown state"):
        ORDER.check("pending_payment", "shipped")


def test_a_redirect_state_is_not_a_paid_state():
    """`payment_verification_pending` must never be mistaken for payment."""
    assert "payment_verification_pending" != "paid"
    assert ORDER.allows("payment_verification_pending", "paid")
    assert ORDER.allows("payment_verification_pending", "cancelled")


# ================================================================== totals


def test_stage_totals_are_computed_not_supplied(core):
    stage = core["checkout"].stage(
        customer_id=ALICE, cart_id="cart-1", cart_state_version=1,
        lines=[{"variant_id": "var-1", "quantity": 2, "unit_price_minor": 249900},
               {"variant_id": "var-2", "quantity": 1, "unit_price_minor": 99900}],
        fulfillment_option="standard", shipping_minor=9900, tax_minor=10000, discount_minor=5000)

    assert stage["subtotal_minor"] == 2 * 249900 + 99900
    assert stage["total_minor"] == stage["subtotal_minor"] + 9900 + 10000 - 5000
    assert sum(line["amount_minor"] for line in stage["lines"]) == stage["subtotal_minor"]


def test_a_discount_cannot_exceed_the_total(core):
    with pytest.raises(ValueError, match="discount"):
        stage_for(core, unit_price=1000, discount_minor=999999)


def test_order_totals_match_the_stage_they_came_from(core):
    stage = stage_for(core, quantity=2)
    core["inventory"].receive("var-1", "loc-blr", 10)

    order = core["checkout"].confirm(stage_id=stage["id"], customer_id=ALICE,
                                     current_cart_state_version=1)

    assert order["total_minor"] == stage["total_minor"]
    assert sum(line["amount_minor"] for line in order["lines"]) == order["subtotal_minor"]
    assert order["amount_paid_minor"] == 0


# =============================================================== ownership


def test_another_customer_cannot_confirm_your_checkout(core):
    stage = stage_for(core, customer_id=ALICE)
    core["inventory"].receive("var-1", "loc-blr", 10)

    with pytest.raises(PermissionError):
        core["checkout"].confirm(stage_id=stage["id"], customer_id=BOB,
                                 current_cart_state_version=1)

    assert core["checkout"].read_stage(stage["id"])["state"] == "staged"
    assert core["store"].rows("SELECT id FROM commerce_orders") == []


def test_another_customer_cannot_open_a_payment_attempt(core):
    core["inventory"].receive("var-1", "loc-blr", 10)
    stage = stage_for(core)
    order = core["checkout"].confirm(stage_id=stage["id"], customer_id=ALICE,
                                     current_cart_state_version=1)

    with pytest.raises(PermissionError):
        core["checkout"].open_attempt(order_id=order["id"], customer_id=BOB)


def test_orders_list_is_scoped_to_the_customer(core):
    core["inventory"].receive("var-1", "loc-blr", 10)
    stage = stage_for(core)
    core["checkout"].confirm(stage_id=stage["id"], customer_id=ALICE, current_cart_state_version=1)

    assert len(core["checkout"].orders_for(ALICE)) == 1
    assert core["checkout"].orders_for(BOB) == []


# =================================================================== stock


def test_staging_reserves_no_stock(core):
    core["inventory"].receive("var-1", "loc-blr", 3)
    stage_for(core)

    assert core["inventory"].sellable("var-1") == 3
    assert core["store"].rows("SELECT id FROM inventory_reservations") == []


def test_confirmation_reserves_stock(core):
    core["inventory"].receive("var-1", "loc-blr", 3)
    stage = stage_for(core, quantity=2)

    core["checkout"].confirm(stage_id=stage["id"], customer_id=ALICE, current_cart_state_version=1)

    assert core["inventory"].sellable("var-1") == 1
    assert core["inventory"].levels("var-1")[0]["on_hand"] == 3  # nothing has shipped yet


def test_confirmation_without_stock_leaves_no_order_and_no_hold(core):
    core["inventory"].receive("var-1", "loc-blr", 1)
    stage = stage_for(core, quantity=5)

    with pytest.raises(InsufficientStock):
        core["checkout"].confirm(stage_id=stage["id"], customer_id=ALICE, current_cart_state_version=1)

    assert core["store"].rows("SELECT id FROM commerce_orders") == []
    assert core["store"].rows("SELECT id FROM inventory_reservations") == []
    assert core["inventory"].sellable("var-1") == 1


def test_two_confirmations_cannot_both_take_the_last_unit(core):
    core["inventory"].receive("var-1", "loc-blr", 1)
    first = stage_for(core, customer_id=ALICE)
    second = core["checkout"].stage(
        customer_id=BOB, cart_id="cart-2", cart_state_version=1,
        lines=[{"variant_id": "var-1", "quantity": 1, "unit_price_minor": 249900}],
        fulfillment_option="standard")

    core["checkout"].confirm(stage_id=first["id"], customer_id=ALICE, current_cart_state_version=1)
    with pytest.raises(InsufficientStock):
        core["checkout"].confirm(stage_id=second["id"], customer_id=BOB, current_cart_state_version=1)

    assert core["inventory"].sellable("var-1") == 0


def test_stock_spans_locations(core):
    core["inventory"].receive("var-1", "loc-blr", 1)
    core["inventory"].receive("var-1", "loc-del", 4)
    stage = stage_for(core, quantity=3)

    order = core["checkout"].confirm(stage_id=stage["id"], customer_id=ALICE,
                                     current_cart_state_version=1)

    held = core["store"].rows(
        "SELECT location_id,quantity FROM inventory_reservations WHERE order_id=?", (order["id"],))
    assert held == [{"location_id": "loc-del", "quantity": 3}]  # BLR cannot cover 3


def test_cancelling_returns_stock(core):
    core["inventory"].receive("var-1", "loc-blr", 3)
    stage = stage_for(core, quantity=2)
    order = core["checkout"].confirm(stage_id=stage["id"], customer_id=ALICE,
                                     current_cart_state_version=1)

    core["checkout"].cancel(order["id"], reason="Customer abandoned the payment")

    assert core["inventory"].sellable("var-1") == 3
    assert core["inventory"].reconcile("var-1")["balanced"]


def test_expired_reservations_return_stock(core):
    core["inventory"].receive("var-1", "loc-blr", 3)
    stage = stage_for(core, quantity=2)
    core["checkout"].confirm(stage_id=stage["id"], customer_id=ALICE, current_cart_state_version=1)
    assert core["inventory"].sellable("var-1") == 1

    expired = core["inventory"].expire_due(now="2999-01-01T00:00:00+00:00")

    assert len(expired) == 1
    assert core["inventory"].sellable("var-1") == 3
    assert core["inventory"].reconcile("var-1")["balanced"]


def test_payment_consumes_stock_permanently(core):
    core["inventory"].receive("var-1", "loc-blr", 3)
    stage = stage_for(core, quantity=2)
    order = core["checkout"].confirm(stage_id=stage["id"], customer_id=ALICE,
                                     current_cart_state_version=1)
    attempt = core["checkout"].open_attempt(order_id=order["id"], customer_id=ALICE)
    core["checkout"].attach_provider_link(attempt["id"], provider_reference="plink_1",
                                          link_url="https://rzp.io/x", snapshot={})

    core["checkout"].settle_from_provider(
        attempt_id=attempt["id"], provider_reference="plink_1",
        amount_minor=order["total_minor"], currency="INR", succeeded=True, snapshot={"ok": True})

    levels = core["inventory"].levels("var-1")[0]
    assert (levels["on_hand"], levels["reserved"]) == (1, 0)
    assert core["inventory"].reconcile("var-1")["balanced"]


def test_inventory_reconciles_against_its_movement_ledger(core):
    core["inventory"].receive("var-1", "loc-blr", 10)
    core["inventory"].receive("var-1", "loc-blr", 5, reason="return")

    assert core["inventory"].levels("var-1")[0]["on_hand"] == 15
    assert core["inventory"].reconcile("var-1")["balanced"]

    # Corrupt the level directly; reconciliation must notice.
    core["store"].execute("UPDATE inventory_levels SET on_hand=99 WHERE variant_id='var-1'")
    assert not core["inventory"].reconcile("var-1")["balanced"]


# ================================================== staging and expiry


def test_a_newer_stage_supersedes_the_older_one(core):
    first = stage_for(core)
    stage_for(core, version=2)

    assert core["checkout"].read_stage(first["id"])["state"] == "superseded"


def test_an_expired_stage_cannot_be_confirmed(core):
    core["inventory"].receive("var-1", "loc-blr", 5)
    stage = stage_for(core, minutes=-1)

    with pytest.raises(StageExpired):
        core["checkout"].confirm(stage_id=stage["id"], customer_id=ALICE, current_cart_state_version=1)

    assert core["checkout"].read_stage(stage["id"])["state"] == "expired"
    assert core["store"].rows("SELECT id FROM commerce_orders") == []


def test_a_changed_cart_invalidates_the_preview(core):
    core["inventory"].receive("var-1", "loc-blr", 5)
    stage = stage_for(core, version=1)

    with pytest.raises(StageMismatch):
        core["checkout"].confirm(stage_id=stage["id"], customer_id=ALICE, current_cart_state_version=2)


def test_a_stage_cannot_be_confirmed_twice(core):
    core["inventory"].receive("var-1", "loc-blr", 5)
    stage = stage_for(core)
    core["checkout"].confirm(stage_id=stage["id"], customer_id=ALICE, current_cart_state_version=1)

    with pytest.raises(TransitionError):
        core["checkout"].confirm(stage_id=stage["id"], customer_id=ALICE, current_cart_state_version=1)

    assert len(core["store"].rows("SELECT id FROM commerce_orders")) == 1


# ================================================= payment verification


def test_one_order_gets_one_live_attempt(core):
    core["inventory"].receive("var-1", "loc-blr", 5)
    stage = stage_for(core)
    order = core["checkout"].confirm(stage_id=stage["id"], customer_id=ALICE,
                                     current_cart_state_version=1)

    first = core["checkout"].open_attempt(order_id=order["id"], customer_id=ALICE)
    second = core["checkout"].open_attempt(order_id=order["id"], customer_id=ALICE)

    assert first["id"] == second["id"]
    assert len(core["store"].rows("SELECT id FROM payment_attempts")) == 1
    # And exactly one provider call was scheduled.
    assert len(core["store"].rows("SELECT id FROM outbox_messages")) == 1


def test_the_outbox_carries_the_attempt_as_its_idempotency_key(core):
    """Keyed on the attempt, not just the order: a redelivery of one attempt's
    message cannot double-create its link, but a genuinely new attempt (a retry
    after a decline) gets its own key and its own link, rather than recovering a
    link Razorpay may have already cancelled or expired."""
    core["inventory"].receive("var-1", "loc-blr", 5)
    stage = stage_for(core)
    order = core["checkout"].confirm(stage_id=stage["id"], customer_id=ALICE,
                                     current_cart_state_version=1)
    attempt = core["checkout"].open_attempt(order_id=order["id"], customer_id=ALICE)

    message = core["store"].rows("SELECT topic,payload FROM outbox_messages")[0]
    assert message["topic"] == "razorpay.payment_link.create"
    assert (json.loads(message["payload"])["idempotency_key"]
            == f"order:{order['id']}:{attempt['id']}")


def test_a_redirect_alone_never_marks_an_order_paid(core):
    core["inventory"].receive("var-1", "loc-blr", 5)
    stage = stage_for(core)
    order = core["checkout"].confirm(stage_id=stage["id"], customer_id=ALICE,
                                     current_cart_state_version=1)

    updated = core["checkout"].mark_verification_pending(order["id"])

    assert updated["status"] == "payment_verification_pending"
    assert updated["amount_paid_minor"] == 0


@pytest.mark.parametrize("bad", [
    {"amount_minor": 100},
    {"currency": "USD"},
    {"provider_reference": "plink_other"},
])
def test_a_mismatched_provider_outcome_is_never_applied(core, bad):
    core["inventory"].receive("var-1", "loc-blr", 5)
    stage = stage_for(core)
    order = core["checkout"].confirm(stage_id=stage["id"], customer_id=ALICE,
                                     current_cart_state_version=1)
    attempt = core["checkout"].open_attempt(order_id=order["id"], customer_id=ALICE)
    core["checkout"].attach_provider_link(attempt["id"], provider_reference="plink_1",
                                          link_url="https://rzp.io/x", snapshot={})

    payload = {"attempt_id": attempt["id"], "provider_reference": "plink_1",
               "amount_minor": order["total_minor"], "currency": "INR",
               "succeeded": True, "snapshot": {}}
    payload.update(bad)

    with pytest.raises(PaymentVerificationMismatch):
        core["checkout"].settle_from_provider(**payload)

    assert core["checkout"].read_order(order["id"])["status"] == "pending_payment"


def test_a_failed_payment_leaves_the_order_unpaid_and_the_stock_held(core):
    core["inventory"].receive("var-1", "loc-blr", 3)
    stage = stage_for(core)
    order = core["checkout"].confirm(stage_id=stage["id"], customer_id=ALICE,
                                     current_cart_state_version=1)
    attempt = core["checkout"].open_attempt(order_id=order["id"], customer_id=ALICE)
    core["checkout"].attach_provider_link(attempt["id"], provider_reference="plink_1",
                                          link_url="https://rzp.io/x", snapshot={})

    updated = core["checkout"].settle_from_provider(
        attempt_id=attempt["id"], provider_reference="plink_1", amount_minor=order["total_minor"],
        currency="INR", succeeded=False, snapshot={}, failure_reason="card_declined")

    assert updated["status"] == "pending_payment"
    assert core["inventory"].sellable("var-1") == 2  # still held for the retry


def test_a_verified_payment_marks_the_order_paid_once(core):
    core["inventory"].receive("var-1", "loc-blr", 5)
    stage = stage_for(core)
    order = core["checkout"].confirm(stage_id=stage["id"], customer_id=ALICE,
                                     current_cart_state_version=1)
    attempt = core["checkout"].open_attempt(order_id=order["id"], customer_id=ALICE)
    core["checkout"].attach_provider_link(attempt["id"], provider_reference="plink_1",
                                          link_url="https://rzp.io/x", snapshot={})

    paid = core["checkout"].settle_from_provider(
        attempt_id=attempt["id"], provider_reference="plink_1", amount_minor=order["total_minor"],
        currency="INR", succeeded=True, snapshot={"status": "paid"})

    assert paid["status"] == "paid"
    assert paid["amount_paid_minor"] == order["total_minor"]

    # A redelivered outcome cannot settle the same attempt twice.
    with pytest.raises(TransitionError):
        core["checkout"].settle_from_provider(
            attempt_id=attempt["id"], provider_reference="plink_1",
            amount_minor=order["total_minor"], currency="INR", succeeded=True, snapshot={})


# ============================================================= inbox dedup


def test_a_redelivered_webhook_is_stored_once(core):
    payload = {"event": "payment_link.paid", "id": "evt_1"}
    first, is_new_first = core["inbox"].receive(
        provider="razorpay", provider_event_id="evt_1", event_type="payment_link.paid", payload=payload)
    second, is_new_second = core["inbox"].receive(
        provider="razorpay", provider_event_id="evt_1", event_type="payment_link.paid", payload=payload)

    assert is_new_first and not is_new_second
    assert first["id"] == second["id"]
    assert len(core["store"].rows("SELECT id FROM inbox_events")) == 1


def test_an_event_is_processed_only_once(core):
    event, _ = core["inbox"].receive(provider="razorpay", provider_event_id="evt_2",
                                    event_type="payment_link.paid", payload={})
    core["inbox"].mark(event["id"], "processed")

    with pytest.raises(TransitionError):
        core["inbox"].mark(event["id"], "processed")


def test_quarantining_requires_a_reason(core):
    event, _ = core["inbox"].receive(provider="razorpay", provider_event_id="evt_3",
                                    event_type="payment_link.paid", payload={})

    with pytest.raises(ValueError, match="reason"):
        core["inbox"].mark(event["id"], "quarantined")

    core["inbox"].mark(event["id"], "quarantined", "order total did not match")
    assert core["inbox"].pending() == []


# ================================================================= outbox


def test_a_claimed_message_is_not_claimed_again(core):
    core["outbox"].enqueue(topic="t", payload={"a": 1})

    first = core["outbox"].claim()
    second = core["outbox"].claim()

    assert len(first) == 1 and second == []
    assert first[0]["payload"] == {"a": 1}


def test_a_failed_message_is_retried_then_dead_lettered(core):
    message_id = core["outbox"].enqueue(topic="t", payload={})
    outbox = Outbox(core["store"], max_attempts=2)

    outbox.claim()
    assert outbox.failed(message_id, "boom", retry_in_seconds=0) == "pending"
    outbox.claim()
    assert outbox.failed(message_id, "boom again", retry_in_seconds=0) == "dead_letter"
    assert outbox.claim() == []


def test_the_outbox_commits_with_the_change_that_caused_it(core):
    """A rolled-back internal change must not leave an external effect scheduled."""
    store = core["store"]
    with pytest.raises(RuntimeError):
        with store.transaction() as tx:
            core["outbox"].enqueue(topic="t", payload={}, tx=tx)
            raise RuntimeError("the internal write failed")

    assert store.rows("SELECT id FROM outbox_messages") == []


# =============================================================== evidence


def test_evidence_records_refusals_as_well_as_successes(core):
    core["inventory"].receive("var-1", "loc-blr", 1)
    stage = stage_for(core, quantity=5)

    with pytest.raises(InsufficientStock):
        core["checkout"].confirm(stage_id=stage["id"], customer_id=ALICE, current_cart_state_version=1)

    outcomes = {row["action"]: row["outcome"] for row in
                core["store"].rows("SELECT action,outcome FROM evidence_records")}
    assert outcomes["stage_checkout"] == "applied"
    assert outcomes["confirm_checkout"] == "blocked"


def test_one_correlation_links_the_whole_journey(core):
    correlation = Correlation()
    core["inventory"].receive("var-1", "loc-blr", 5)
    stage = stage_for(core, correlation=correlation)
    order = core["checkout"].confirm(stage_id=stage["id"], customer_id=ALICE,
                                     current_cart_state_version=1, correlation=correlation)
    core["checkout"].open_attempt(order_id=order["id"], customer_id=ALICE, correlation=correlation)

    lineage = core["ledger"].for_correlation(correlation.correlation_id)

    assert [row["action"] for row in lineage] == [
        "stage_checkout", "confirm_checkout", "open_payment_attempt"]
    assert core["store"].rows(
        "SELECT correlation_id FROM outbox_messages")[0]["correlation_id"] == correlation.correlation_id


def test_evidence_rejects_an_unknown_outcome_or_origin(core):
    with pytest.raises(ValueError, match="outcome"):
        core["ledger"].record(actor=Actor("system"), action="a", reason="r", outcome="succeeded")
    with pytest.raises(ValueError, match="origin"):
        core["ledger"].record(actor=Actor("system"), action="a", reason="r", outcome="applied",
                              data_origin="production")


def test_commerce_events_are_the_basis_for_derived_totals(core):
    core["inventory"].receive("var-1", "loc-blr", 5)
    stage = stage_for(core, quantity=2)
    order = core["checkout"].confirm(stage_id=stage["id"], customer_id=ALICE,
                                     current_cart_state_version=1)
    attempt = core["checkout"].open_attempt(order_id=order["id"], customer_id=ALICE)
    core["checkout"].attach_provider_link(attempt["id"], provider_reference="p1",
                                          link_url="u", snapshot={})
    core["checkout"].settle_from_provider(attempt_id=attempt["id"], provider_reference="p1",
                                          amount_minor=order["total_minor"], currency="INR",
                                          succeeded=True, snapshot={})

    paid = core["store"].rows(
        "SELECT SUM(amount_minor) AS total FROM commerce_events WHERE event_type='order_paid'")
    assert paid[0]["total"] == order["total_minor"]


# ====================================================== merchant changes


def test_staging_a_change_never_applies_it(core):
    change = core["changes"].stage(
        operator_id=OPERATOR, kind="price_update", target_type="catalog_variant", target_id="var-1",
        before={"amount_minor": 249900}, after={"amount_minor": 229900},
        rationale="Slow sell-through this month")

    assert change["status"] == "pending"
    assert change["applied_at"] is None


def test_no_repository_method_stages_and_applies_together(core):
    """The agent-reachable verb cannot reach `applied` in one call."""
    change = core["changes"].stage(
        operator_id=OPERATOR, kind="price_update", target_type="catalog_variant", target_id="var-1",
        before={"amount_minor": 249900}, after={"amount_minor": 229900}, rationale="Slow sales")

    applied = []
    with pytest.raises(TransitionError):
        core["changes"].apply(change_id=change["id"], operator_id=OPERATOR,
                              applier=lambda c, tx: applied.append(c))

    assert applied == []


def test_an_out_of_bounds_proposal_is_blocked_at_staging(core):
    with pytest.raises(PolicyViolation, match="exceeds"):
        core["changes"].stage(
            operator_id=OPERATOR, kind="price_update", target_type="catalog_variant",
            target_id="var-1", before={"amount_minor": 249900}, after={"amount_minor": 10000},
            rationale="Far too large a cut")

    assert core["store"].rows("SELECT id FROM merchant_changes") == []
    blocked = core["store"].rows(
        "SELECT outcome FROM evidence_records WHERE action='stage_merchant_change'")
    assert blocked[0]["outcome"] == "blocked"


def test_policy_is_revalidated_at_application_time(core):
    change = core["changes"].stage(
        operator_id=OPERATOR, kind="price_update", target_type="catalog_variant", target_id="var-1",
        before={"amount_minor": 249900}, after={"amount_minor": 229900}, rationale="Slow sales")
    core["changes"].decide(change_id=change["id"], operator_id=OPERATOR, decision="approved")

    # The world moved: the recorded `before` no longer passes the bound.
    core["store"].execute(
        "UPDATE merchant_changes SET before_doc=? WHERE id=?",
        (json.dumps({"amount_minor": 1000000}), change["id"]))

    with pytest.raises(PolicyViolation):
        core["changes"].apply(change_id=change["id"], operator_id=OPERATOR,
                              applier=lambda c, tx: None)

    assert core["changes"].read(change["id"])["status"] == "failed"


def test_an_approved_change_applies_once_with_evidence(core):
    change = core["changes"].stage(
        operator_id=OPERATOR, kind="price_update", target_type="catalog_variant", target_id="var-1",
        before={"amount_minor": 249900}, after={"amount_minor": 229900}, rationale="Slow sales")
    core["changes"].decide(change_id=change["id"], operator_id=OPERATOR, decision="approved",
                           note="Agreed, within the seasonal band")

    def applier(c, tx):
        tx.execute(
            "INSERT INTO variant_prices (id,variant_id,amount_minor,valid_from) VALUES (?,?,?,?)",
            ("prc-1", c["target_id"], c["after"]["amount_minor"], "2026-09-04T00:00:00+00:00"))

    applied = core["changes"].apply(change_id=change["id"], operator_id=OPERATOR, applier=applier)

    assert applied["status"] == "applied"
    assert core["store"].rows("SELECT amount_minor FROM variant_prices")[0]["amount_minor"] == 229900
    with pytest.raises(TransitionError):
        core["changes"].apply(change_id=change["id"], operator_id=OPERATOR, applier=applier)


def test_a_rejected_change_is_terminal(core):
    change = core["changes"].stage(
        operator_id=OPERATOR, kind="inventory_action", target_type="catalog_variant",
        target_id="var-1", before={"units": 0}, after={"units": 50}, rationale="Restock")
    core["changes"].decide(change_id=change["id"], operator_id=OPERATOR, decision="rejected")

    with pytest.raises(TransitionError):
        core["changes"].decide(change_id=change["id"], operator_id=OPERATOR, decision="approved")


# ============================================================== schema


def test_typed_specs_reject_an_ambiguous_value(core):
    core["store"].execute(
        "INSERT INTO variant_specs (variant_id,spec_key,value_numeric,value_unit) "
        "VALUES ('var-1','output_watts',30,'W')")

    with pytest.raises(Exception):
        # Two typed columns set at once: the value has no single type.
        core["store"].execute(
            "INSERT INTO variant_specs (variant_id,spec_key,value_text,value_numeric) "
            "VALUES ('var-1','colour','graphite',5)")


def test_money_is_stored_in_minor_units_as_integers(core):
    core["inventory"].receive("var-1", "loc-blr", 5)
    stage = stage_for(core, unit_price=249900)

    assert isinstance(stage["total_minor"], int)
    with pytest.raises(Exception):
        core["store"].execute(
            "INSERT INTO variant_prices (id,variant_id,amount_minor,valid_from) VALUES (?,?,?,?)",
            ("prc-bad", "var-1", -100, "2026-09-04T00:00:00+00:00"))


def test_reserved_can_never_exceed_on_hand(core):
    core["inventory"].receive("var-1", "loc-blr", 2)

    with pytest.raises(Exception):
        core["store"].execute(
            "UPDATE inventory_levels SET reserved=5 WHERE variant_id='var-1'")
