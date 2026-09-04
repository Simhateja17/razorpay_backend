"""Run the Phase 6 merchant paths against the live Supabase Postgres.

The pytest suite runs on SQLite, and a green SQLite suite is not evidence: dialect
differences have shipped broken code from this repo before, and this phase adds a
lot of new SQL — day truncation, grouped joins across the catalogue, price history
windows, and the current-state reads the host revalidates against. So the reads,
the staging bounds, the approval, the stale-proposal refusal and the application
run here, against the real database and the real seeded ninety days.

Everything this creates is removable: every probe row carries the `ph6_` prefix,
the operator id is `ph6_operator`, and the run deletes its own changes, approvals,
evidence, price rows and inventory movements on the way out — including on
failure. It borrows no real principal and it touches no order.

    PYTHONPATH=. .venv/bin/python scripts/verify_phase6_live.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import traceback
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from marketplace_backend.evidence import EvidenceLedger  # noqa: E402
from marketplace_backend.merchant import DecisionRefused, MerchantService  # noqa: E402
from marketplace_backend.merchant_changes import MerchantChangeRepository  # noqa: E402
from marketplace_backend.metrics import MetricsRepository  # noqa: E402
from marketplace_backend.store import Store  # noqa: E402

from cartisan_agent import CoreMerchantPort, MerchantAgentConfig  # noqa: E402
from cartisan_agent.merchant_types import MerchantSessionContext  # noqa: E402
from cartisan_agent.outcomes import BusinessRefusal  # noqa: E402

PROBE_PREFIX = "ph6"
OPERATOR = f"{PROBE_PREFIX}_operator"


class Probe:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.ledger = EvidenceLedger(store)
        self.changes = MerchantChangeRepository(store, self.ledger)
        self.config = MerchantAgentConfig()
        self.port = CoreMerchantPort(
            store, changes=self.changes, metrics=MetricsRepository(store), config=self.config)
        self.service = MerchantService(store, self.port, self.changes, self.ledger)
        self.session = MerchantSessionContext(
            conversation_id=f"{PROBE_PREFIX}-probe", customer_id=OPERATOR)
        self.passed: list[str] = []
        self.change_ids: list[str] = []
        self.touched_variants: list[str] = []
        self.touched_products: list[str] = []

    def check(self, name: str, condition: bool, detail: str = "") -> None:
        if not condition:
            raise AssertionError(f"{name}: {detail or 'failed'}")
        self.passed.append(name)
        print(f"  ok  {name}")

    # -- setup ---------------------------------------------------------------

    def pick_variant(self) -> tuple[str, str, int]:
        """A real seeded variant that has actually sold, so the estimates have a rate
        to work from rather than dividing by zero."""
        rows = self.store.rows(
            "SELECT l.variant_id AS variant_id, v.product_id AS product_id, "
            "SUM(l.quantity) AS units FROM commerce_order_lines l "
            "JOIN commerce_orders o ON o.id = l.order_id "
            "JOIN catalog_variants v ON v.id = l.variant_id "
            "WHERE o.status='paid' AND o.origin='seeded' "
            "GROUP BY l.variant_id, v.product_id ORDER BY units DESC LIMIT 1")
        if not rows:
            raise SystemExit("no seeded paid order lines; reseed first")
        variant_id = rows[0]["variant_id"]
        return variant_id, rows[0]["product_id"], self.port._price(variant_id)

    # -- the checks ----------------------------------------------------------

    async def run(self) -> None:
        variant_id, product_id, price = self.pick_variant()
        self.touched_variants.append(variant_id)
        self.touched_products.append(product_id)
        print(f"\nprobe variant {variant_id} at {price} paise\n")

        await self.reads(variant_id, product_id)
        await self.staging_and_approval(variant_id, price)
        await self.staleness(variant_id, price)

    async def reads(self, variant_id: str, product_id: str) -> None:
        print("reads:")
        snapshot = await self.port.get_business_snapshot(self.session, 30)
        claims = {claim.key: claim for claim in snapshot.claims}
        self.check("the snapshot derives from the seeded event log",
                   claims["paid_orders"].value > 0, str(claims["paid_orders"].value))
        revenue = claims["net_revenue_minor"]
        # The dialect bug this catches: a numeric column coming back as Decimal from
        # Postgres and as int from SQLite would break this arithmetic, not the query.
        self.check("net revenue reproduces from its own inputs",
                   revenue.value == revenue.inputs["gross_minor"] - revenue.inputs["refunded_minor"],
                   f"{revenue.value} vs {revenue.inputs}")
        self.check("every snapshot figure is observed, none causal",
                   all(claim.claim_kind == "observed" for claim in snapshot.claims))
        self.check("the snapshot covers seeded origin only",
                   snapshot.origins == ["seeded"], str(snapshot.origins))

        # Day truncation is dialect-specific (`SUBSTR` on SQLite, a `date` cast on
        # Postgres), so a daily series is exactly the kind of thing SQLite cannot prove.
        daily = await self.port.query_metrics(self.session, "revenue", 30)
        self.check("the daily revenue series has points", len(daily.points) > 0,
                   str(len(daily.points)))
        self.check("the series total equals the sum of its points",
                   daily.total == sum(point.value for point in daily.points))
        self.check("each point's day is a date, not a timestamp",
                   all(len(str(point.date)) == 10 for point in daily.points),
                   str(daily.points[0].date))

        grouped = await self.port.query_metrics(self.session, "units", 30, "category")
        self.check("units group by category across the catalogue join",
                   len(grouped.points) > 0, str(len(grouped.points)))

        for metric in ("orders", "conversion", "refund_rate", "cart_abandonment"):
            series = await self.port.query_metrics(self.session, metric, 30)
            self.check(f"{metric} derives without error", series.basis != "")

        listings = await self.port.search_listings(self.session, "charger", limit=5)
        self.check("listing search returns the operator's own catalogue", len(listings) > 0)
        listing = await self.port.get_listing(self.session, product_id)
        self.check("a listing carries its variants with live stock",
                   listing is not None and len(listing.variants) > 0)

        pricing = await self.port.get_pricing_context(self.session, variant_id)
        self.check("pricing context carries a price history",
                   pricing is not None and len(pricing.history) > 0)
        self.check("the bound is stated as a floor and a ceiling",
                   pricing.floor_minor < pricing.current_price_minor < pricing.ceiling_minor,
                   f"{pricing.floor_minor} {pricing.current_price_minor} {pricing.ceiling_minor}")

        campaigns = await self.port.get_campaign_performance(self.session)
        self.check("campaign spend is observed and attribution is declared missing",
                   campaigns and any("not recorded" in note.lower() or "NOT available" in note
                                     for note in campaigns[0].limitations),
                   str(campaigns[0].limitations if campaigns else None))

        alerts = await self.port.get_inventory_alerts(self.session, limit=5)
        self.check("inventory alerts read without error and carry their formula",
                   all(alert.days_of_cover is not None and alert.days_of_cover.inputs
                       for alert in alerts),
                   f"{len(alerts)} alerts")

    async def staging_and_approval(self, variant_id: str, price: int) -> None:
        print("\nstaging, approval and application:")
        over_bound = int(price * 0.5)
        try:
            await self.port.stage_change(
                self.session, kind="price_update", target_type="catalog_variant",
                target_id=variant_id, before={"amount_minor": price},
                after={"amount_minor": over_bound}, rationale=f"{PROBE_PREFIX} bound probe")
            raise AssertionError("a 50% cut was staged; the bound did not hold")
        except BusinessRefusal as refused:
            self.check("a change outside the bound is refused at staging",
                       "exceeds the 25% bound" in str(refused))
        self.check("a refused staging wrote no change row",
                   self.store.rows(
                       "SELECT COUNT(*) AS n FROM merchant_changes WHERE operator_id=?",
                       (OPERATOR,))[0]["n"] == 0)

        inside = int(price * 0.95)
        change = await self.port.stage_change(
            self.session, kind="price_update", target_type="catalog_variant",
            target_id=variant_id, before={"amount_minor": price},
            after={"amount_minor": inside}, rationale=f"{PROBE_PREFIX} live approval probe")
        self.change_ids.append(change.change_id)
        self.check("staging produces a pending change and nothing else",
                   change.status == "pending", change.status)
        self.check("staging changed no price",
                   self.port._price(variant_id) == price)

        rejected = await self.port.stage_change(
            self.session, kind="price_update", target_type="catalog_variant",
            target_id=variant_id, before={"amount_minor": price},
            after={"amount_minor": int(price * 0.97)},
            rationale=f"{PROBE_PREFIX} live rejection probe")
        self.change_ids.append(rejected.change_id)
        decided = self.service.decide(
            operator_id=OPERATOR, change_id=rejected.change_id, decision="rejected",
            note=f"{PROBE_PREFIX} not this time")
        self.check("a rejection is terminal", decided["status"] == "rejected")
        record = self.store.rows(
            "SELECT * FROM evidence_records WHERE action='rejected_merchant_change' "
            "AND target_id=?", (rejected.change_id,))
        self.check("the rejection carries the documents it rejected", len(record) == 1)
        state = self.store.load(record[0]["state_ref"])
        self.check("the rejected before-and-after are exact",
                   state["before"] == {"amount_minor": price}
                   and state["after"] == {"amount_minor": int(price * 0.97)},
                   str(state))

        applied = self.service.decide(
            operator_id=OPERATOR, change_id=change.change_id, decision="approved")
        self.check("an approved change that still holds is applied",
                   applied["status"] == "applied", applied["status"])
        self.check("the price in force is the approved one",
                   self.port._price(variant_id) == inside,
                   f"{self.port._price(variant_id)} vs {inside}")
        self.check("the previous price was closed rather than overwritten",
                   self.store.rows(
                       "SELECT COUNT(*) AS n FROM variant_prices WHERE variant_id=? "
                       "AND valid_to IS NOT NULL", (variant_id,))[0]["n"] >= 1)
        lineage = self.store.rows(
            "SELECT action FROM evidence_records WHERE correlation_id=(SELECT correlation_id "
            "FROM evidence_records WHERE action='approved_merchant_change' AND target_id=?) "
            "ORDER BY recorded_at", (change.change_id,))
        self.check("the approval and the application share one lineage",
                   [row["action"] for row in lineage]
                   == ["approved_merchant_change", "apply_merchant_change"],
                   str([row["action"] for row in lineage]))

        # Put the price back before anything else reads it.
        self.restore_price(variant_id, price)
        self.check("the probe's price change is reverted",
                   self.port._price(variant_id) == price)

    async def staleness(self, variant_id: str, price: int) -> None:
        print("\nrevalidation against current state:")
        stale = await self.port.stage_change(
            self.session, kind="price_update", target_type="catalog_variant",
            target_id=variant_id, before={"amount_minor": price},
            after={"amount_minor": int(price * 0.95)},
            rationale=f"{PROBE_PREFIX} staleness probe")
        self.change_ids.append(stale.change_id)
        # The world moves between the staging and the decision.
        moved = int(price * 0.90)
        self.service._apply_price(self.store, variant_id, {"amount_minor": moved})
        try:
            self.service.decide(
                operator_id=OPERATOR, change_id=stale.change_id, decision="approved")
            raise AssertionError("a stale proposal was applied")
        except DecisionRefused as refused:
            self.check("a proposal whose record moved is refused at application",
                       "moved after this change was staged" in str(refused), str(refused))
        self.check("the stale change is marked failed",
                   self.service.change(stale.change_id)["status"] == "failed")
        self.check("the refusal wrote nothing: the price is the one the world moved to",
                   self.port._price(variant_id) == moved,
                   f"{self.port._price(variant_id)} vs {moved}")
        checks = self.store.load(self.store.rows(
            "SELECT policy_checks FROM evidence_records WHERE action='apply_merchant_change' "
            "AND target_id=? ORDER BY recorded_at DESC LIMIT 1", (stale.change_id,)
        )[0]["policy_checks"])
        self.check("the refusal records both the staged and the current figures",
                   checks["staged_before"] == {"amount_minor": price}
                   and checks["current_before"] == {"amount_minor": moved},
                   str(checks))

        self.restore_price(variant_id, price)
        self.check("the price is back where the probe found it",
                   self.port._price(variant_id) == price)

    def restore_price(self, variant_id: str, price: int) -> None:
        """Close every price row this run opened and reopen the original.

        The probe's rows all carry the `ph6_` prefix, so cleanup removes them; this
        puts the catalogue back to the price it was found at in the meantime, so no
        later check in this run reads a price the probe invented.
        """
        with self.store.transaction() as tx:
            tx.execute(
                "DELETE FROM variant_prices WHERE variant_id=? AND id LIKE ? ESCAPE '!'",
                (variant_id, f"price!_{PROBE_PREFIX}%"))
            tx.execute(
                "DELETE FROM variant_prices WHERE variant_id=? AND valid_to IS NULL "
                "AND id NOT LIKE 'sd!_%' ESCAPE '!'", (variant_id,))
            tx.execute(
                "UPDATE variant_prices SET valid_to=NULL WHERE variant_id=? AND id LIKE 'sd!_%' "
                "ESCAPE '!' AND amount_minor=?", (variant_id, price))

    # -- cleanup -------------------------------------------------------------

    def cleanup(self) -> None:
        print("\ncleaning up probe rows")

        def wipe(sql: str, params: tuple = ()) -> None:
            try:
                self.store.execute(sql, params)
            except Exception as exc:  # noqa: BLE001 - cleanup reports, never raises
                print(f"  !! {sql.split()[2]}: {exc}")

        for variant_id in self.touched_variants:
            wipe("DELETE FROM inventory_movements WHERE reference_type='merchant_change' "
                 "AND reference_id IN (SELECT id FROM merchant_changes WHERE operator_id=?)",
                 (OPERATOR,))
            wipe("DELETE FROM variant_prices WHERE variant_id=? AND id NOT LIKE 'sd!_%' ESCAPE '!'",
                 (variant_id,))
            wipe("UPDATE variant_prices SET valid_to=NULL WHERE variant_id=? AND id LIKE 'sd!_%' "
                 "ESCAPE '!' AND valid_to IS NOT NULL", (variant_id,))
        for change_id in self.change_ids:
            wipe("DELETE FROM evidence_records WHERE target_id=?", (change_id,))
            wipe("DELETE FROM merchant_approvals WHERE change_id=?", (change_id,))
        wipe("DELETE FROM evidence_records WHERE actor_id=?", (OPERATOR,))
        wipe("DELETE FROM merchant_changes WHERE operator_id=?", (OPERATOR,))
        left = self.store.rows(
            "SELECT COUNT(*) AS n FROM merchant_changes WHERE operator_id=?", (OPERATOR,))
        print(f"cleanup complete ({left[0]['n']} probe changes left)")


def main() -> int:
    url = os.getenv("SUPABASE_DATABASE_URL")
    if not url:
        raise SystemExit("SUPABASE_DATABASE_URL is not set; this script only runs live")
    store = Store(database_url=url)
    print(f"connected to {store.backend}")
    if store.backend not in {"postgres", "supabase"}:
        raise SystemExit("refusing to run: this must exercise Postgres, not SQLite")

    probe = Probe(store)
    failed = None
    try:
        asyncio.run(probe.run())
    except Exception:  # noqa: BLE001 - reported below, after cleanup runs
        failed = traceback.format_exc()
    finally:
        probe.cleanup()

    if failed:
        print("\nFAILED\n")
        print(failed)
        return 1
    print(f"\n{len(probe.passed)} live checks passed against Postgres")
    return 0


if __name__ == "__main__":
    sys.exit(main())
