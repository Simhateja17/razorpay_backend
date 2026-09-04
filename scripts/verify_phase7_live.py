"""Run the Phase 7 observability paths against the live Supabase Postgres.

A green SQLite suite is not evidence, and this phase is mostly new SQL: grouped
aggregates over the ledger, `SUM(CASE WHEN ...)` roll-ups, a six-way merge across
tables whose timestamps come back as `datetime` from Postgres and as `str` from
SQLite, and filters on columns that were added by migration rather than created
with their tables. Every one of those is a place the two dialects can disagree.

What it proves, in order:

  * the lineage columns exist live and are actually written by the real code path;
  * one whole journey — cart, stage, confirm, attempt, provider event — shares one
    correlation id through the production repositories;
  * the journey view assembles it from all six sources, in order, over Postgres;
  * the failure journey (a quarantined callback) reads the same way and leaves the
    order unpaid;
  * filtering by principal and by demo run excludes everything else;
  * the seeded scenario packs are followable, which is what a judge is pointed at;
  * every health claim computes over the real ninety days without raising, and
    states its window.

Everything it creates is removable: every probe row carries the `ph7` demo run,
the customer is `ph7_customer`, and the run deletes its own order, attempt, stage,
cart, inbox row, outbox message, evidence and events on the way out — including on
failure. It borrows no real principal, and the provider call is a stub: no real
Razorpay request is made.

    PYTHONPATH=. .venv/bin/python scripts/verify_phase7_live.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
import traceback
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from marketplace_backend.checkout import CheckoutRepository  # noqa: E402
from marketplace_backend.evidence import (  # noqa: E402
    CommerceEventLog,
    Correlation,
    EvidenceLedger,
    Inbox,
    Outbox,
)
from marketplace_backend.health import HealthMetrics  # noqa: E402
from marketplace_backend.inventory import InventoryRepository  # noqa: E402
from marketplace_backend.observability import EvidenceView  # noqa: E402
from marketplace_backend.payments import PaymentLinkDispatcher, WebhookProcessor  # noqa: E402
from marketplace_backend.recovery import RecoveryService  # noqa: E402
from marketplace_backend.shopping import ShoppingService  # noqa: E402
from marketplace_backend.store import Store  # noqa: E402

from cartisan_agent import CoreCommercePort  # noqa: E402
from cartisan_agent.config import CartisanAgentConfig  # noqa: E402

DEMO_RUN = "ph7_verification"

# The probe's principals are real Supabase Auth accounts, created here and deleted on
# the way out.
#
# They have to be. `cartisan.customers.id` is a foreign key to `auth.users`, and
# `customer_carts.customer_id` is a foreign key to that — neither constraint exists
# in the SQLite schema the suite runs against, so a probe that invented a uuid would
# pass every test and fail live. Going through the admin API is also the honest
# thing: it exercises the same identity chain a real shopper does (ADR 0010).
PROBE_EMAILS = ("ph7-probe-a@example.test", "ph7-probe-b@example.test")


class StubGateway:
    """A provider stand-in. The Razorpay round trip is proven by Phase 5's live run
    and by test mode's own rate limits; what this script has to prove is that the
    lineage survives Postgres, which needs no real provider call."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def create_payment_link(self, *, amount: int, reference_id: str, description: str) -> dict:
        self.calls.append(reference_id)
        index = len(self.calls)
        return {"id": f"plink_ph7_{index}", "short_url": f"https://rzp.io/ph7/{index}",
                "amount": amount, "currency": "INR"}


class ProbeIdentities:
    """Two throwaway Supabase accounts, created and destroyed by this run."""

    def __init__(self) -> None:
        self.url = os.getenv("SUPABASE_URL", "").rstrip("/")
        self.key = os.getenv("SUPABASE_SERVICE_KEY", "")
        if not self.url or not self.key:
            raise SystemExit(
                "SUPABASE_URL and SUPABASE_SERVICE_KEY are needed: the probe's principals "
                "are real auth users, because `customers.id` is a foreign key to auth.users")
        self.headers = {"apikey": self.key, "Authorization": f"Bearer {self.key}",
                        "Content-Type": "application/json"}
        self.ids: list[str] = []

    def create(self) -> tuple[str, str]:
        for email in PROBE_EMAILS:
            self.ids.append(self._create_one(email))
        return self.ids[0], self.ids[1]

    def _create_one(self, email: str) -> str:
        response = httpx.post(
            f"{self.url}/auth/v1/admin/users", headers=self.headers,
            json={"email": email, "password": f"ph7-{uuid.uuid4().hex}", "email_confirm": True,
                  "app_metadata": {"cartisan_role": "customer"}}, timeout=30)
        if response.status_code < 300:
            return response.json()["id"]
        # A previous run that died before cleanup leaves the account behind; reuse it
        # rather than refusing to run.
        existing = self._find(email)
        if existing:
            return existing
        raise SystemExit(f"could not create probe identity {email}: "
                         f"{response.status_code} {response.text}")

    def _find(self, email: str) -> str | None:
        response = httpx.get(f"{self.url}/auth/v1/admin/users", headers=self.headers,
                             params={"page": 1, "per_page": 200}, timeout=30)
        if response.status_code >= 300:
            return None
        for user in response.json().get("users", []):
            if user.get("email") == email:
                return user["id"]
        return None

    def destroy(self) -> None:
        for user_id in self.ids:
            try:
                httpx.delete(f"{self.url}/auth/v1/admin/users/{user_id}",
                             headers=self.headers, timeout=30)
            except Exception as exc:  # noqa: BLE001 - cleanup reports, never raises
                print(f"  !! could not delete probe identity {user_id}: {exc}")


class Probe:
    def __init__(self, store: Store, customer: str, other: str) -> None:
        self.customer, self.other = customer, other
        self.store = store
        self.ledger = EvidenceLedger(store)
        self.outbox, self.inbox = Outbox(store), Inbox(store)
        self.checkout = CheckoutRepository(
            store, InventoryRepository(store), self.ledger, self.outbox, CommerceEventLog(store))
        self.port = CoreCommercePort(store, checkout=self.checkout, config=CartisanAgentConfig())
        self.gateway = StubGateway()
        self.dispatcher = PaymentLinkDispatcher(
            store, self.checkout, self.outbox, self.gateway, self.ledger)
        self.service = ShoppingService(store, self.port, self.checkout, self.dispatcher, self.ledger)
        self.webhooks = WebhookProcessor(store, self.checkout, self.inbox, self.ledger)
        self.view = EvidenceView(store)
        self.health = HealthMetrics(store)
        self.recovery = RecoveryService(store, self.checkout, self.ledger)
        self.passed: list[str] = []
        self.order_ids: list[str] = []
        self.correlations: list[str] = []
        # Exact levels for the variant this run touches, read before anything moves.
        # Restoring them is the only reliable way back: a confirmation reserves, a
        # paid settlement consumes, and unpicking that arithmetic by hand is how the
        # first version of this cleanup left two units reserved against nothing.
        self.levels_before: list[dict] = []

    def check(self, name: str, condition: bool, detail: str = "") -> None:
        if not condition:
            raise AssertionError(f"{name}: {detail or 'failed'}")
        self.passed.append(name)
        print(f"  ok  {name}")

    # -- setup ---------------------------------------------------------------

    def ensure_customers(self) -> None:
        """`customer_carts` has a real foreign key to `customers` live, which SQLite's
        schema does not carry — so a probe that only wrote `seed_customers` would pass
        every test and fail here. The rows are removed again on the way out."""
        for customer_id, email in zip((self.customer, self.other), PROBE_EMAILS):
            if not self.store.rows("SELECT id FROM customers WHERE id=?", (customer_id,)):
                self.store.execute(
                    "INSERT INTO customers (id,email,display_name,origin,created_at) "
                    "VALUES (?,?,?,'live_app',now())", (customer_id, email, "Phase 7 probe"))

    def pick_variant(self) -> str:
        """A seeded variant with real sellable stock, so a confirmation can reserve."""
        rows = self.store.rows(
            "SELECT l.variant_id AS variant_id, SUM(l.on_hand - l.reserved) AS sellable "
            "FROM inventory_levels l JOIN catalog_variants v ON v.id = l.variant_id "
            "WHERE v.status='active' GROUP BY l.variant_id "
            "HAVING SUM(l.on_hand - l.reserved) >= 4 ORDER BY sellable DESC LIMIT 1")
        if not rows:
            raise SystemExit("no variant with spare stock; reseed first")
        return rows[0]["variant_id"]

    async def buy(self, variant_id: str, customer: str, correlation: Correlation) -> dict:
        await self.service.add(customer, variant_id, 1, correlation=correlation)
        stage = await self.service.stage(customer, correlation=correlation)
        confirmed = await self.service.confirm(
            customer, stage["stage_id"], correlation=correlation)
        self.order_ids.append(confirmed["order"]["order_id"])
        self.correlations.append(correlation.correlation_id)
        return confirmed

    # -- the checks ----------------------------------------------------------

    async def run(self) -> None:
        self.ensure_customers()
        variant_id = self.pick_variant()
        self.levels_before = self.store.rows(
            "SELECT variant_id,location_id,on_hand,reserved FROM inventory_levels "
            "WHERE variant_id=?", (variant_id,))
        print(f"\nprobe variant {variant_id}\n")

        self.columns_exist()
        golden = await self.golden_journey(variant_id)
        await self.failure_journey(variant_id)
        await self.isolation(variant_id)
        self.seeded_scenarios()
        self.health_claims()
        self.recovery_queue()
        print(f"\ngolden journey: {golden}")

    def columns_exist(self) -> None:
        print("schema:")
        for table, column in (
            ("commerce_orders", "correlation_id"), ("commerce_orders", "demo_run_id"),
            ("payment_attempts", "correlation_id"), ("inbox_events", "correlation_id"),
            ("turns", "correlation_id"), ("turns", "demo_run_id"),
        ):
            rows = self.store.rows(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='cartisan' AND table_name=? AND column_name=?",
                (table, column))
            self.check(f"{table}.{column} exists live", bool(rows))

    async def golden_journey(self, variant_id: str) -> str:
        print("\ngolden journey:")
        correlation = Correlation(demo_run_id=DEMO_RUN)
        confirmed = await self.buy(variant_id, self.customer, correlation)
        order_id = confirmed["order"]["order_id"]

        order = self.store.rows("SELECT * FROM commerce_orders WHERE id=?", (order_id,))[0]
        self.check("the order stores the request's lineage",
                   order["correlation_id"] == correlation.correlation_id,
                   str(order["correlation_id"]))
        self.check("the order stores the demo run", order["demo_run_id"] == DEMO_RUN)

        attempt = self.store.rows(
            "SELECT * FROM payment_attempts WHERE order_id=?", (order_id,))[0]
        self.check("the payment attempt continues the same lineage",
                   attempt["correlation_id"] == correlation.correlation_id)
        self.check("the provider link was attached", bool(attempt["provider_link_url"]))

        # The provider's answer arrives on its own request knowing only a reference.
        outcome = self.webhooks.process({
            "id": f"evt_ph7_paid_{order_id[-6:]}", "event": "payment_link.paid",
            "payload": {"payment_link": {"entity": {
                "id": attempt["provider_reference"], "amount": order["total_minor"],
                "currency": "INR", "status": "paid"}}}})
        self.check("the verified event was applied", outcome["result"] == "applied",
                   str(outcome))
        event = self.store.rows(
            "SELECT * FROM inbox_events WHERE id=?", (outcome["inbox_id"],))[0]
        self.check("the provider event rejoined the journey",
                   event["correlation_id"] == correlation.correlation_id,
                   str(event["correlation_id"]))

        journey = self.view.journey(correlation.correlation_id)
        sources = {step["source"] for step in journey["steps"]}
        self.check("the journey assembles from every source",
                   {"evidence", "order", "payment_attempt", "provider_event"} <= sources,
                   str(sorted(sources)))
        # The dialect bug this catches: Postgres returns `datetime` where SQLite
        # returns ISO text, so an unnormalised sort would raise or order wrongly.
        stamps = [step["at"] for step in journey["steps"]]
        self.check("every journey step has a text timestamp",
                   all(isinstance(at, str) for at in stamps), str(stamps[:2]))
        self.check("the journey is ordered oldest first", stamps == sorted(stamps))
        self.check("the journey reports the paid order",
                   journey["orders"][0]["status"] == "paid", str(journey["orders"]))

        actions = {row["action"] for row in
                   self.view.records(correlation_id=correlation.correlation_id, limit=200)}
        self.check("one lineage covers cart through settlement",
                   {"add_to_cart", "stage_checkout", "confirm_checkout",
                    "open_payment_attempt", "create_payment_link",
                    "settle_payment_attempt"} <= actions, str(sorted(actions)))
        return correlation.correlation_id

    async def failure_journey(self, variant_id: str) -> None:
        print("\nfailure journey:")
        correlation = Correlation(demo_run_id=DEMO_RUN)
        confirmed = await self.buy(variant_id, self.customer, correlation)
        order_id = confirmed["order"]["order_id"]
        attempt = self.store.rows(
            "SELECT * FROM payment_attempts WHERE order_id=?", (order_id,))[0]

        outcome = self.webhooks.process({
            "id": f"evt_ph7_bad_{order_id[-6:]}", "event": "payment_link.paid",
            "payload": {"payment_link": {"entity": {
                "id": attempt["provider_reference"], "amount": 1,
                "currency": "INR", "status": "paid"}}}})
        self.check("a mismatched amount is quarantined", outcome["result"] == "quarantined",
                   str(outcome))
        order = self.store.rows("SELECT * FROM commerce_orders WHERE id=?", (order_id,))[0]
        self.check("the quarantined order is not paid", order["status"] != "paid",
                   order["status"])
        self.check("nothing was recorded as received", int(order["amount_paid_minor"]) == 0)

        journey = self.view.journey(correlation.correlation_id)
        blocked = [step for step in journey["steps"] if step["outcome"] == "blocked"]
        self.check("the refusal is visible in the journey", bool(blocked))
        provider = [s for s in journey["steps"] if s["source"] == "provider_event"]
        self.check("the quarantined event carries its reason",
                   bool(provider) and bool(provider[0]["detail"]["quarantine_reason"]),
                   str(provider[:1]))

        quarantined = [row for row in self.recovery.quarantined(limit=50)
                       if row["inbox_id"] == outcome["inbox_id"]]
        self.check("the quarantine appears in the recovery queue", bool(quarantined))
        self.check("the only offered action is acknowledge",
                   quarantined[0]["recovery_actions"] == ["acknowledge"],
                   str(quarantined[0]["recovery_actions"]))

    async def isolation(self, variant_id: str) -> None:
        print("\nno unrelated-session noise:")
        other = Correlation(demo_run_id="ph7_other_run")
        await self.buy(variant_id, self.other, other)

        mine = self.view.records(actor_id=self.customer, limit=200)
        self.check("a principal's view holds only their own rows",
                   {row["actor_id"] for row in mine} == {self.customer},
                   str({row["actor_id"] for row in mine}))

        run_rows = self.view.records(demo_run_id=DEMO_RUN, limit=500)
        self.check("a demo run's view holds only that run",
                   {row["demo_run_id"] for row in run_rows} == {DEMO_RUN})
        self.check("the demo run excludes the other run's lineage",
                   other.correlation_id not in {row["correlation_id"] for row in run_rows})

        # 348 seeded paid orders live in the same tables. A view that could not
        # exclude them is the "unrelated-session noise" the acceptance forbids.
        self.check("the filtered view is far smaller than the whole ledger",
                   len(run_rows) < int(self.store.rows(
                       "SELECT COUNT(*) AS n FROM evidence_records")[0]["n"]))

        journeys = self.view.journeys(demo_run_id=DEMO_RUN, limit=20)
        self.check("journeys list only this run's lineages",
                   all(row["demo_run_id"] == DEMO_RUN for row in journeys), str(len(journeys)))
        self.check("each journey summary names its origins",
                   all(row["origins"] for row in journeys))

        runs = {row["demo_run_id"] for row in self.view.demo_runs(limit=100)}
        self.check("the demo run picker lists this run", DEMO_RUN in runs)

    def seeded_scenarios(self) -> None:
        """A judge following a seeded story and a live one should be doing the same
        thing, so the scenario packs have to be followable the same way."""
        print("\nseeded scenarios:")
        seeded = self.store.rows(
            "SELECT DISTINCT correlation_id FROM evidence_records "
            "WHERE data_origin='razorpay_test' AND correlation_id IS NOT NULL "
            "AND demo_run_id IS NULL LIMIT 5")
        # Packs installed before this phase carry a lineage but no demo run; packs
        # installed after a reseed carry both. Either way the lineage has to join up.
        self.check("scenario evidence carries a correlation id", bool(seeded), str(seeded))
        if seeded:
            journey = self.view.journey(seeded[0]["correlation_id"])
            self.check("a seeded scenario assembles as a journey", journey["found"])
            self.check("its steps are ordered",
                       [s["at"] for s in journey["steps"]] ==
                       sorted(s["at"] for s in journey["steps"]))

        named = self.store.rows(
            "SELECT DISTINCT demo_run_id FROM evidence_records "
            "WHERE demo_run_id LIKE 'scenario:%'")
        print(f"  -- {len(named)} named scenario runs present "
              f"(reseed to label the existing packs)")

    def health_claims(self) -> None:
        print("\nhealth metrics:")
        report = self.health.report(hours=24 * 120)
        claims = [claim for group in ("runtime", "tools", "payments", "delivery")
                  for claim in report[group]]
        self.check("every group produced claims", len(claims) >= 12, str(len(claims)))
        self.check("every claim states its basis", all(claim["basis"] for claim in claims))
        self.check("every claim states its window",
                   all("window_hours" in claim["inputs"] for claim in claims))
        for claim in claims:
            if claim["inputs"]["window_hours"] is None:
                self.check(f"{claim['key']} declares that it is not windowed",
                           any("windowed" in note for note in claim["limitations"]))
        self.check("every claim is observed, none causal",
                   all(claim["claim_kind"] == "observed" for claim in claims))

        payments = {claim["key"]: claim for claim in report["payments"]}
        # Postgres returns SUM() as Decimal; a ratio computed from one would be a
        # Decimal too, and would not survive JSON. This is that check.
        for key in ("verified_payment_rate", "provider_event_quarantine_rate"):
            value = payments[key]["value"]
            self.check(f"{key} is a plain number or None",
                       value is None or isinstance(value, float), f"{value!r}")
        self.check("orders created is an int",
                   isinstance(payments["orders_created"]["value"], int))

        # A ratio above 1 reads as a percentage over 100% and is a wrong claim. This
        # caught `prompt_cache_read_rate` reporting 243% against real cached turns,
        # where the seeded fixtures had too few tokens to show it.
        for claim in claims:
            if claim["unit"] == "ratio" and claim["value"] is not None:
                self.check(f"{claim['key']} is a real share",
                           0.0 <= claim["value"] <= 1.0, str(claim["value"]))
        self.check("origin counts come back labelled",
                   all(row["data_origin"] in {"seeded", "live_app", "razorpay_test"}
                       for row in report["origins"]), str(report["origins"]))

        runtime = {claim["key"]: claim for claim in report["runtime"]}
        self.check("turns are counted over the live table",
                   isinstance(runtime["turns_started"]["value"], int))

    def recovery_queue(self) -> None:
        print("\nrecovery queue:")
        queue = self.recovery.queue(limit=50)
        self.check("the queue has all four sections",
                   set(queue) == {"dead_letters", "quarantined", "unprocessed", "stuck_orders"})
        for row in queue["stuck_orders"]:
            self.check(f"stuck order {row['order_id']} names its actions",
                       bool(row["recovery_actions"]))
            break
        else:
            print("  -- no stuck orders live right now")
        for row in queue["unprocessed"]:
            self.check("an undecided event names reprocess",
                       row["recovery_actions"] == ["reprocess_event"])
            break

    # -- cleanup -------------------------------------------------------------

    def cleanup(self) -> None:
        print("\ncleaning up probe rows")

        def wipe(sql: str, params: tuple = ()) -> None:
            try:
                self.store.execute(sql, params)
            except Exception as exc:  # noqa: BLE001 - cleanup reports, never raises
                print(f"  !! {sql.split()[2]}: {exc}")

        # Children before parents, and reservations released before the order goes,
        # or the stock this probe held would never come back.
        for order_id in self.order_ids:
            # A consume movement references the RESERVATION, not the order, so
            # deleting by order id alone leaves the sale behind and the variant stops
            # reconciling. Reservation ids are read before the reservations go.
            reservations = [row["id"] for row in self.store.rows(
                "SELECT id FROM inventory_reservations WHERE order_id=?", (order_id,))]
            for reservation_id in reservations:
                wipe("DELETE FROM inventory_movements WHERE reference_id=?", (reservation_id,))
            wipe("DELETE FROM inventory_reservations WHERE order_id=?", (order_id,))
            wipe("DELETE FROM inventory_movements WHERE reference_id=?", (order_id,))
            wipe("DELETE FROM payment_attempts WHERE order_id=?", (order_id,))
            wipe("DELETE FROM commerce_order_lines WHERE order_id=?", (order_id,))
            wipe("DELETE FROM commerce_orders WHERE id=?", (order_id,))
        for correlation_id in self.correlations:
            wipe("DELETE FROM inbox_events WHERE correlation_id=?", (correlation_id,))
            wipe("DELETE FROM outbox_messages WHERE correlation_id=?", (correlation_id,))
            wipe("DELETE FROM commerce_events WHERE correlation_id=?", (correlation_id,))
            wipe("DELETE FROM evidence_records WHERE correlation_id=?", (correlation_id,))
        for customer in (self.customer, self.other):
            wipe("DELETE FROM checkout_stage_lines WHERE stage_id IN "
                 "(SELECT id FROM checkout_stages WHERE customer_id=?)", (customer,))
            wipe("DELETE FROM checkout_stages WHERE customer_id=?", (customer,))
            wipe("DELETE FROM cart_lines WHERE cart_id IN "
                 "(SELECT id FROM customer_carts WHERE customer_id=?)", (customer,))
            wipe("DELETE FROM customer_carts WHERE customer_id=?", (customer,))
            wipe("DELETE FROM idempotency_records WHERE principal_id=?", (customer,))
            wipe("DELETE FROM evidence_records WHERE actor_id=?", (customer,))
            wipe("DELETE FROM commerce_events WHERE customer_id=?", (customer,))
            wipe("DELETE FROM customers WHERE id=?", (customer,))
        wipe("DELETE FROM evidence_records WHERE demo_run_id IN (?,?)",
             (DEMO_RUN, "ph7_other_run"))

        # Put the stock back exactly as it was found, after the reservations and
        # movements that moved it are gone.
        for level in self.levels_before:
            wipe("UPDATE inventory_levels SET on_hand=?, reserved=? "
                 "WHERE variant_id=? AND location_id=?",
                 (level["on_hand"], level["reserved"], level["variant_id"],
                  level["location_id"]))
        if self.levels_before:
            variant_id = self.levels_before[0]["variant_id"]
            balance = InventoryRepository(self.store).reconcile(variant_id)
            print(f"  inventory reconciles for {variant_id}: {balance['balanced']}"
                  + ("" if balance["balanced"] else f" — {balance['problems']}"))

        left = self.store.rows(
            "SELECT COUNT(*) AS n FROM evidence_records WHERE demo_run_id IN (?,?)",
            (DEMO_RUN, "ph7_other_run"))[0]["n"]
        orders_left = self.store.rows(
            "SELECT COUNT(*) AS n FROM commerce_orders WHERE customer_id IN (?,?)",
            (self.customer, self.other))[0]["n"]
        print(f"cleanup complete ({left} probe evidence rows, {orders_left} probe orders left)")


def main() -> int:
    url = os.getenv("SUPABASE_DATABASE_URL")
    if not url:
        raise SystemExit("SUPABASE_DATABASE_URL is not set; this script only runs live")
    store = Store(database_url=url)
    print(f"connected to {store.backend}")
    if store.backend not in {"postgres", "supabase"}:
        raise SystemExit("refusing to run: this must exercise Postgres, not SQLite")

    identities = ProbeIdentities()
    customer, other = identities.create()
    print(f"probe identities: {customer}, {other}")

    probe = Probe(store, customer, other)
    failed = None
    try:
        asyncio.run(probe.run())
    except Exception:  # noqa: BLE001 - reported below, after cleanup runs
        failed = traceback.format_exc()
    finally:
        probe.cleanup()
        identities.destroy()

    if failed:
        print("\nFAILED\n")
        print(failed)
        return 1
    print(f"\n{len(probe.passed)} live checks passed against Postgres")
    return 0


if __name__ == "__main__":
    sys.exit(main())
