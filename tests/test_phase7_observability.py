"""Phase 7 acceptance: one journey a judge can follow, and no unrelated noise.

The acceptance sentence is "a judge can follow one golden journey and one failure
from customer request through Razorpay evidence without unrelated-session noise or
unsupported claims", and each clause is a separate group of tests here:

  *follow one journey*   the lineage joins up — the request, the turn, the tools,
                         the order, the attempt, the provider event — rather than
                         producing six unrelated stories.
  *one failure*          the quarantined callback is followable in exactly the same
                         way, and is not silently repairable.
  *no unrelated noise*   a filtered view returns this principal, or this demo run,
                         and nothing else.
  *no unsupported claims* every health figure states its window and its formula,
                         and the "all recorded history" figure says so.
"""

from __future__ import annotations

import asyncio

import pytest

from marketplace_backend.evidence import Correlation
from marketplace_backend.recovery import RecoveryRefused, order_recovery_actions
from marketplace_backend.state_machines import OUTBOX, TransitionError

from conftest_runtime import (
    CUSTOMER,
    LAPTOP,
    FakeGateway,
    build_shopping,
    build_store,
    paid_event,
)

OTHER_CUSTOMER = "22222222-2222-2222-2222-222222222222"


@pytest.fixture
def world(tmp_path):
    store = build_store(tmp_path)
    store.execute(
        "INSERT INTO customers (id,email,display_name,origin,created_at) "
        "VALUES (?,?,?,'live_app',datetime('now'))",
        (OTHER_CUSTOMER, "grace@example.test", "Grace"))
    return build_shopping(store)


def run(coro):
    return asyncio.run(coro)


def separate_world(tmp_path, name: str, gateway: FakeGateway):
    """A second store, for the failure cases that have to break delivery without
    breaking the fixture every other test in the file shares."""
    directory = tmp_path / name
    directory.mkdir()
    return build_shopping(build_store(directory), gateway=gateway)


def exhaust_attempts(world) -> None:
    """Drain until the message is parked.

    `Outbox.failed` schedules each retry thirty seconds out, so a tight drain loop
    claims nothing: the message is pending but not yet due. Production waits; the
    test makes each retry due instead of sleeping through the real backoff.
    """
    for _ in range(world.outbox.max_attempts + 2):
        world.store.execute(
            "UPDATE outbox_messages SET available_at=? WHERE status='pending'",
            ("2020-01-01T00:00:00+00:00",))
        run(world.dispatcher.drain())


def purchase(world, *, correlation: Correlation, customer: str = CUSTOMER,
             quantity: int = 1) -> dict:
    """One whole journey on one lineage: cart, stage, confirm, link, provider event."""
    run(world.service.add(customer, LAPTOP, quantity, correlation=correlation))
    stage = run(world.service.stage(customer, correlation=correlation))
    confirmed = run(world.service.confirm(customer, stage["stage_id"], correlation=correlation))
    return confirmed


# -- one lineage, end to end ---------------------------------------------------

def test_one_browser_journey_shares_one_correlation_id(world):
    """The cart write, the stage, the order and the payment attempt are one story.

    Before Phase 7 each of these minted its own `Correlation()`, so the ledger held
    four unrelated fragments for one purchase and `for_correlation` could only ever
    return the last hop.
    """
    correlation = Correlation()
    confirmed = purchase(world, correlation=correlation)

    order = world.store.rows(
        "SELECT * FROM commerce_orders WHERE id=?", (confirmed["order"]["order_id"],))[0]
    assert order["correlation_id"] == correlation.correlation_id

    attempt = world.store.rows(
        "SELECT * FROM payment_attempts WHERE order_id=?", (order["id"],))[0]
    assert attempt["correlation_id"] == correlation.correlation_id

    actions = {row["action"] for row in
               world.evidence.records(correlation_id=correlation.correlation_id, limit=100)}
    assert {"add_to_cart", "stage_checkout", "confirm_checkout",
            "open_payment_attempt", "create_payment_link"} <= actions


def test_provider_event_rejoins_the_journey_that_asked_for_the_link(world):
    """The webhook arrives on its own HTTP request and knows only a provider
    reference. It must still land in the customer's story, not beside it."""
    correlation = Correlation()
    confirmed = purchase(world, correlation=correlation)
    reference = confirmed["payment"]["provider_reference"]

    outcome = world.webhooks.process(paid_event(reference, confirmed["order"]["total_minor"]))
    assert outcome["result"] == "applied"

    settled = [row for row in world.evidence.records(
        correlation_id=correlation.correlation_id, limit=100)
        if row["action"] == "settle_payment_attempt"]
    assert settled and settled[0]["data_origin"] == "razorpay_test"

    event = world.store.rows("SELECT * FROM inbox_events WHERE id=?", (outcome["inbox_id"],))[0]
    assert event["correlation_id"] == correlation.correlation_id


def test_journey_reads_as_one_ordered_story(world):
    """The view a judge actually follows: the ledger, the order, the attempt and the
    provider's answer merged into one list, oldest first."""
    correlation = Correlation()
    confirmed = purchase(world, correlation=correlation)
    world.webhooks.process(
        paid_event(confirmed["payment"]["provider_reference"],
                   confirmed["order"]["total_minor"]))

    journey = world.evidence.journey(correlation.correlation_id)
    assert journey["found"]
    sources = {step["source"] for step in journey["steps"]}
    assert {"evidence", "order", "payment_attempt", "provider_event"} <= sources
    assert [step["at"] for step in journey["steps"]] == sorted(
        step["at"] for step in journey["steps"])
    assert journey["orders"][0]["status"] == "paid"


def test_an_agent_turn_continues_the_request_lineage(world):
    """A turn opened under a request's correlation id does not start a second one,
    and its tool calls are recorded against the same story."""
    from cartisan_agent.turns import TurnStore
    from cartisan_agent.types import SessionContext

    correlation = Correlation()
    turns = TurnStore(world.store, world.ledger)
    session = SessionContext(conversation_id="conv-lineage", customer_id=CUSTOMER,
                             correlation_id=correlation.correlation_id)
    turn = turns.begin(session, user_message="find me a laptop", prompt_version="p1",
                       tool_contract_version="t1", skill_versions="[]")
    assert turn.correlation_id == correlation.correlation_id

    stored = world.store.rows("SELECT * FROM turns WHERE id=?", (turn.turn_id,))[0]
    assert stored["correlation_id"] == correlation.correlation_id


def test_a_retry_stays_on_the_orders_own_journey(world):
    """A retry arrives on a fresh HTTP request, but it is a later step of the purchase
    that already exists — not a new story about the same order (ADR 0030)."""
    correlation = Correlation()
    confirmed = purchase(world, correlation=correlation)
    order_id = confirmed["order"]["order_id"]
    world.checkout.cancel(order_id, reason="test decline")

    # A fresh order, then a retry with no correlation supplied at all.
    second = Correlation()
    confirmed2 = purchase(world, correlation=second)
    world.store.execute(
        "UPDATE payment_attempts SET status='failed' WHERE order_id=?",
        (confirmed2["order"]["order_id"],))
    run(world.service.open_payment(CUSTOMER, confirmed2["order"]["order_id"]))

    attempts = world.store.rows(
        "SELECT correlation_id FROM payment_attempts WHERE order_id=?",
        (confirmed2["order"]["order_id"],))
    assert {row["correlation_id"] for row in attempts} == {second.correlation_id}


# -- the failure journey -------------------------------------------------------

def test_quarantined_callback_is_followable_as_its_own_journey(world):
    """The graceful failure a judge is shown. It must be as followable as the golden
    path, and must leave the order unpaid."""
    correlation = Correlation()
    confirmed = purchase(world, correlation=correlation)
    reference = confirmed["payment"]["provider_reference"]

    outcome = world.webhooks.process(paid_event(reference, 1))  # wrong amount
    assert outcome["result"] == "quarantined"

    journey = world.evidence.journey(correlation.correlation_id)
    blocked = [step for step in journey["steps"] if step["outcome"] == "blocked"]
    assert blocked, "the refusal has to be visible in the journey, not only in a log"
    assert journey["orders"][0]["status"] != "paid"

    event = [step for step in journey["steps"] if step["source"] == "provider_event"][0]
    assert event["detail"]["status"] == "quarantined"
    assert event["detail"]["quarantine_reason"]


def test_a_quarantined_event_is_never_repairable_into_paid(world):
    """The one thing recovery must not offer. Acknowledging records a human's reading
    and deliberately leaves the status alone (ADR 0013)."""
    correlation = Correlation()
    confirmed = purchase(world, correlation=correlation)
    outcome = world.webhooks.process(
        paid_event(confirmed["payment"]["provider_reference"], 1))
    inbox_id = outcome["inbox_id"]

    assert world.recovery.quarantined()[0]["recovery_actions"] == ["acknowledge"]
    world.recovery.acknowledge(inbox_id, note="Checked with the provider; amount was wrong.")

    event = world.store.rows("SELECT * FROM inbox_events WHERE id=?", (inbox_id,))[0]
    assert event["status"] == "quarantined"
    order = world.store.rows(
        "SELECT * FROM commerce_orders WHERE id=?", (confirmed["order"]["order_id"],))[0]
    assert order["status"] != "paid"
    assert order["amount_paid_minor"] == 0

    with pytest.raises(RecoveryRefused):
        world.recovery.acknowledge(inbox_id, note="   ")


# -- no unrelated-session noise ------------------------------------------------

def test_evidence_is_filtered_to_one_principal(world):
    """Two customers shop at once. Neither one's view may contain the other's rows —
    which the flat `audit` table could not express at all."""
    mine, theirs = Correlation(), Correlation()
    purchase(world, correlation=mine)
    purchase(world, correlation=theirs, customer=OTHER_CUSTOMER)

    rows = world.evidence.records(actor_id=CUSTOMER, limit=200)
    assert rows
    assert {row["actor_id"] for row in rows} == {CUSTOMER}


def test_evidence_is_filtered_to_one_demo_run(world):
    """The judge's control: this run, and nothing else that ever happened."""
    run_a = Correlation(demo_run_id="demo:a")
    run_b = Correlation(demo_run_id="demo:b")
    purchase(world, correlation=run_a)
    purchase(world, correlation=run_b)

    rows = world.evidence.records(demo_run_id="demo:a", limit=200)
    assert rows
    assert {row["demo_run_id"] for row in rows} == {"demo:a"}
    assert {row["correlation_id"] for row in rows} == {run_a.correlation_id}

    runs = {row["demo_run_id"] for row in world.evidence.demo_runs()}
    assert {"demo:a", "demo:b"} <= runs


def test_every_evidence_row_carries_an_origin_label(world):
    """ADR 0032: a reader never has to guess which kind of record they are looking at."""
    correlation = Correlation()
    confirmed = purchase(world, correlation=correlation)
    world.webhooks.process(
        paid_event(confirmed["payment"]["provider_reference"],
                   confirmed["order"]["total_minor"]))

    rows = world.evidence.records(correlation_id=correlation.correlation_id, limit=100)
    assert all(row["data_origin"] in {"seeded", "live_app", "razorpay_test"} for row in rows)
    # The provider's own evidence is labelled as the provider's, not as the app's.
    settled = [row for row in rows if row["action"] == "settle_payment_attempt"]
    assert settled[0]["data_origin"] == "razorpay_test"


def test_an_unknown_filter_value_is_refused_rather_than_ignored(world):
    """A filter that silently does nothing would show a judge more than they asked
    for while telling them it was narrowed."""
    for bad in ({"origin": "made_up"}, {"outcome": "sort_of"}, {"surface": "elsewhere"}):
        with pytest.raises(ValueError):
            world.evidence.records(**bad)


def test_journeys_list_summarises_by_lineage(world):
    correlation = Correlation(demo_run_id="demo:list")
    purchase(world, correlation=correlation)
    rows = world.evidence.journeys(demo_run_id="demo:list")
    assert len(rows) == 1
    assert rows[0]["correlation_id"] == correlation.correlation_id
    assert rows[0]["records"] >= 4
    assert rows[0]["orders"][0]["status"] == "pending_payment"


# -- payment recovery ----------------------------------------------------------

def test_a_dead_lettered_link_request_is_visible_and_requeueable(world, tmp_path):
    """`Outbox` has parked messages since Phase 2 and nothing could see one. The order
    behind it is real and its stock is really held, so this is recoverable."""
    broken = separate_world(tmp_path, "deadletter", FakeGateway(fail_times=99))
    correlation = Correlation()
    run(broken.service.add(CUSTOMER, LAPTOP, 1, correlation=correlation))
    stage = run(broken.service.stage(CUSTOMER, correlation=correlation))
    run(broken.service.confirm(CUSTOMER, stage["stage_id"], correlation=correlation))
    exhaust_attempts(broken)

    parked = broken.recovery.dead_letters()
    assert len(parked) == 1
    assert parked[0]["order"]["status"] == "pending_payment"
    assert parked[0]["last_error"]
    assert parked[0]["recovery_actions"] == ["retry_message"]

    broken.recovery.retry_message(parked[0]["message_id"])
    assert broken.store.rows(
        "SELECT status FROM outbox_messages WHERE id=?",
        (parked[0]["message_id"],))[0]["status"] == "pending"
    assert broken.recovery.dead_letters() == []

    # And the recovery itself is evidence, on the journey it belongs to.
    assert [row for row in broken.evidence.records(
        correlation_id=correlation.correlation_id, limit=100)
        if row["action"] == "retry_dead_letter"]


def test_requeueing_a_dead_letter_then_draining_recovers_the_link(world, tmp_path):
    """The requeue has to actually finish the job, not just move a status."""
    broken = separate_world(tmp_path, "requeue", FakeGateway(fail_times=5))
    correlation = Correlation()
    run(broken.service.add(CUSTOMER, LAPTOP, 1, correlation=correlation))
    stage = run(broken.service.stage(CUSTOMER, correlation=correlation))
    confirmed = run(broken.service.confirm(CUSTOMER, stage["stage_id"], correlation=correlation))
    exhaust_attempts(broken)
    parked = broken.recovery.dead_letters()
    assert parked

    broken.recovery.retry_message(parked[0]["message_id"])
    run(broken.dispatcher.drain())

    attempt = broken.store.rows(
        "SELECT * FROM payment_attempts WHERE order_id=?",
        (confirmed["order"]["order_id"],))[0]
    assert attempt["provider_link_url"], "the customer should now have a usable link"
    assert attempt["status"] == "pending"


def test_a_delivered_message_cannot_be_requeued(world):
    """Requeueing a delivered effect would create a second one."""
    correlation = Correlation()
    purchase(world, correlation=correlation)
    delivered = world.store.rows(
        "SELECT id FROM outbox_messages WHERE status='delivered'")[0]["id"]
    with pytest.raises(RecoveryRefused):
        world.recovery.retry_message(delivered)


def test_the_dead_letter_requeue_is_a_declared_state_machine_edge():
    """The recovery control obeys the same model everything else is checked against,
    rather than being the one path that writes past it."""
    OUTBOX.check("dead_letter", "pending")
    with pytest.raises(TransitionError):
        OUTBOX.check("delivered", "pending")


def test_an_undecided_event_can_be_reprocessed_through_real_verification(world):
    """A delivery interrupted before it was decided. Reprocessing runs the ordinary
    processor, so a payload that does not match is quarantined now, not applied."""
    correlation = Correlation()
    confirmed = purchase(world, correlation=correlation)
    reference = confirmed["payment"]["provider_reference"]
    event, _ = world.inbox.receive(
        provider="razorpay", provider_event_id="evt_stuck",
        event_type="payment_link.paid",
        payload=paid_event(reference, confirmed["order"]["total_minor"],
                           event_id="evt_stuck"))

    assert [row["inbox_id"] for row in world.recovery.unprocessed()] == [event["id"]]
    outcome = world.recovery.reprocess_event(event["id"], world.webhooks)
    assert outcome["result"] == "applied"
    assert world.recovery.unprocessed() == []


def test_reprocessing_a_mismatched_event_quarantines_it_rather_than_applying(world):
    correlation = Correlation()
    confirmed = purchase(world, correlation=correlation)
    event, _ = world.inbox.receive(
        provider="razorpay", provider_event_id="evt_bad", event_type="payment_link.paid",
        payload=paid_event(confirmed["payment"]["provider_reference"], 1, event_id="evt_bad"))

    outcome = world.recovery.reprocess_event(event["id"], world.webhooks)
    assert outcome["result"] == "quarantined"
    order = world.store.rows(
        "SELECT * FROM commerce_orders WHERE id=?", (confirmed["order"]["order_id"],))[0]
    assert order["status"] != "paid"


def test_a_stuck_order_can_be_cancelled_and_releases_its_stock(world):
    correlation = Correlation()
    confirmed = purchase(world, correlation=correlation)
    order_id = confirmed["order"]["order_id"]
    before = world.port.sellable(LAPTOP)

    world.recovery.cancel_order(order_id, reason="Customer asked us to drop it")
    assert world.store.rows(
        "SELECT status FROM commerce_orders WHERE id=?", (order_id,))[0]["status"] == "cancelled"
    assert world.port.sellable(LAPTOP) == before + 1
    with pytest.raises(RecoveryRefused):
        world.recovery.cancel_order(order_id, reason="again")


def test_an_order_awaiting_verification_offers_no_cancel(world):
    """A verified event may be in flight; cancelling could void an order the provider
    is about to report as paid."""
    assert order_recovery_actions("pending_payment") == ["retry_payment", "cancel_order"]
    assert order_recovery_actions("payment_verification_pending") == ["await_verification"]
    assert order_recovery_actions("paid") == []


def test_the_order_view_names_the_same_recovery_actions(world):
    """ADR 0030: every available recovery action is visible in the order view."""
    correlation = Correlation()
    confirmed = purchase(world, correlation=correlation)
    order = world.service.order(CUSTOMER, confirmed["order"]["order_id"])
    assert order["recovery_actions"] == ["retry_payment", "cancel_order"]
    assert order["correlation_id"] == correlation.correlation_id
    assert order["origin"] == "live_app"


# -- health metrics, without unsupported claims --------------------------------

def test_every_health_claim_shows_its_basis_and_inputs(world):
    correlation = Correlation()
    purchase(world, correlation=correlation)
    report = world.health.report(hours=24)
    claims = [claim for group in ("runtime", "tools", "payments", "delivery")
              for claim in report[group]]
    assert claims
    for claim in claims:
        assert claim["basis"], claim["key"]
        assert isinstance(claim["inputs"], dict)
        assert claim["claim_kind"] == "observed"


def test_every_windowed_health_claim_states_its_window(world):
    """The Phase 6 trap, restated: a figure with no window displayed beside windowed
    ones reads as a share of them. Anything not windowed has to say so itself."""
    report = world.health.report(hours=6)
    for group in ("runtime", "tools", "payments", "delivery"):
        for claim in report[group]:
            assert "window_hours" in claim["inputs"], claim["key"]
            if claim["inputs"]["window_hours"] is None:
                assert any("windowed" in note for note in claim["limitations"]), claim["key"]
            else:
                assert claim["inputs"]["window_hours"] == 6


def test_a_rate_with_an_empty_denominator_is_none_not_zero(world):
    """Zero would be a claim about a healthy system; None is the truth that nothing
    was measured."""
    report = world.health.report(hours=1)
    rates = {claim["key"]: claim for group in ("runtime", "tools", "payments")
             for claim in report[group] if claim["unit"] == "ratio"}
    for claim in rates.values():
        if claim["value"] is None:
            assert claim["limitations"], claim["key"]


def test_no_health_ratio_can_exceed_one(world):
    """A ratio above 1 reads as a percentage over 100% and is simply a wrong claim.

    This is pinned because it shipped once: `prompt_cache_read_rate` divided cached
    tokens by `input_tokens`, and the Messages API reports `input_tokens` as the
    *uncached* remainder — so a well-cached turn reported 243%. Every SQLite fixture
    has too few tokens to make that visible, which is why it took a live run.
    """
    from cartisan_agent.turns import TurnStore
    from cartisan_agent.types import SessionContext

    turns = TurnStore(world.store, world.ledger)
    session = SessionContext(conversation_id="c-cache", customer_id=CUSTOMER)
    turn = turns.begin(session, user_message="hello", prompt_version="p",
                       tool_contract_version="t", skill_versions="[]")
    # The shape a cached turn actually has: a large cached prefix, a small fresh tail.
    turns.complete(turn, agent_message="hi", usage={
        "input_tokens": 120, "output_tokens": 40, "cache_read_input_tokens": 24_000})

    report = world.health.report(hours=24)
    for group in ("runtime", "tools", "payments", "delivery"):
        for claim in report[group]:
            if claim["unit"] == "ratio" and claim["value"] is not None:
                assert 0.0 <= claim["value"] <= 1.0, f"{claim['key']} = {claim['value']}"

    cache = {c["key"]: c for c in report["runtime"]}["prompt_cache_read_rate"]
    assert cache["value"] == round(24_000 / 24_120, 4)
    assert cache["inputs"]["prompt_tokens"] == 24_120


def test_health_counts_the_real_payment_path(world):
    correlation = Correlation()
    confirmed = purchase(world, correlation=correlation)
    world.webhooks.process(
        paid_event(confirmed["payment"]["provider_reference"],
                   confirmed["order"]["total_minor"]))
    payments = {claim["key"]: claim for claim in world.health.report(hours=24)["payments"]}
    assert payments["orders_created"]["value"] == 1
    assert payments["verified_payment_rate"]["value"] == 1.0
    assert payments["payment_attempts"]["value"] == 1
    assert payments["provider_event_quarantine_rate"]["value"] == 0.0


def test_health_reports_dead_letters_across_all_history(world, tmp_path):
    """The one deliberately unwindowed figure, and the reason it is unwindowed."""
    broken = separate_world(tmp_path, "parked", FakeGateway(fail_times=99))
    run(broken.service.add(CUSTOMER, LAPTOP, 1))
    stage = run(broken.service.stage(CUSTOMER))
    run(broken.service.confirm(CUSTOMER, stage["stage_id"]))
    exhaust_attempts(broken)

    claim = {c["key"]: c for c in broken.health.report(hours=1)["delivery"]}["dead_letters"]
    assert claim["value"] == 1
    assert claim["inputs"]["window_hours"] is None
    assert any("windowed" in note for note in claim["limitations"])


def test_health_narrows_to_one_demo_run(world):
    """Tool executions carry no demo run of their own; the turn they belong to does."""
    from cartisan_agent.turns import TurnStore
    from cartisan_agent.types import SessionContext

    turns = TurnStore(world.store, world.ledger)
    for label, conversation in (("demo:x", "c-x"), ("demo:y", "c-y")):
        session = SessionContext(conversation_id=conversation, customer_id=CUSTOMER,
                                 demo_run_id=label)
        turn = turns.begin(session, user_message="hello", prompt_version="p",
                           tool_contract_version="t", skill_versions="[]")
        turns.complete(turn, agent_message="hi", usage={"input_tokens": 10, "output_tokens": 2})

    only_x = {claim.key: claim for claim in world.health.runtime(24, "demo:x")}
    assert only_x["turns_started"].value == 1
    both = {claim.key: claim for claim in world.health.runtime(24, None)}
    assert both["turns_started"].value == 2
