"""Give one variant a real sales rate, so the restock beat has something to stage.

The generated world buys exactly one unit per journey across ~230 variants, so no
variant ever moves fast enough to fall under the ten-day cover the merchant agent
alerts on. That is a property of the generator, not a bug in the agent: with a rate
of 0.1 units/day even two units of stock reads as sixty days of cover.

This adds a fast mover: a short run of ordinary paid journeys for one variant, built
with the same row shape and the same `sd_` prefix the generator uses, plus a stock
level low enough that the observed rate crosses the threshold. Every row carries the
`sd_dm_` prefix, so `--undo` removes exactly what this wrote and a seed reset (which
matches `sd_%`) still removes it too.

    python scripts/seed_restock_demo.py --dry-run
    python scripts/seed_restock_demo.py
    python scripts/seed_restock_demo.py --undo
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from marketplace_backend.store import Store  # noqa: E402

PREFIX = "sd_dm_"
CURRENCY = "INR"
# The numbers the demo reads off the screen. These 27 units sit alongside the three
# the generated history already sold in the window, so the variant reads 30 units
# over the agent's 30-day window — one a day. Six sellable is then six days of cover,
# under the ten-day alert threshold, and the 21-day target sizes the order at fifteen.
# The three inherited units age out of the window over the following days, so the
# figures drift down a little each day; the alert holds either way.
ORDER_QUANTITIES = (3, 2, 2, 2, 3, 2, 2, 2, 3, 2, 2, 2)  # 12 journeys, 27 units
TARGET_SELLABLE = 6


def open_store(sqlite_path: str | None) -> Store:
    if sqlite_path:
        return Store(sqlite_path)
    url = os.getenv("SUPABASE_DATABASE_URL")
    if not url:
        raise SystemExit("Set SUPABASE_DATABASE_URL, or pass --sqlite PATH for a local run.")
    return Store(database_url=url)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S+00:00")


# Written newest-child-first so the deletes below never orphan a parent.
_TABLES = (
    ("fulfillment_lines", "fulfillment_id"),
    ("fulfillments", "id"),
    ("evidence_records", "id"),
    ("commerce_events", "id"),
    ("inbox_events", "id"),
    ("payment_attempts", "id"),
    ("commerce_order_lines", "id"),
    ("commerce_orders", "id"),
    ("checkout_stage_lines", "stage_id"),
    ("checkout_stages", "id"),
    ("presentation_items", "id"),
    ("presentations", "id"),
    ("turns", "id"),
    ("conversations", "id"),
)


def undo(store: Store) -> None:
    for table, column in _TABLES:
        store.execute(f"DELETE FROM {table} WHERE {column} LIKE ?", (f"{PREFIX}%",))


def build(store: Store, variant_id: str, as_of: datetime) -> tuple[int, int]:
    rows = store.rows(
        "SELECT v.id, v.title FROM catalog_variants v JOIN catalog_products p ON p.id = v.product_id "
        "WHERE v.id = ? AND v.status='active' AND p.status='active'", (variant_id,))
    if not rows:
        raise SystemExit(f"{variant_id} is not an active variant of an active product.")
    price_rows = store.rows(
        "SELECT amount_minor FROM variant_prices WHERE variant_id = ? ORDER BY valid_from DESC LIMIT 1",
        (variant_id,))
    if not price_rows:
        raise SystemExit(f"{variant_id} has no price; a line amount would have no source.")
    unit = int(price_rows[0]["amount_minor"])
    customers = [str(r["id"]) for r in store.rows(
        "SELECT id FROM seed_customers ORDER BY id LIMIT ?", (len(ORDER_QUANTITIES),))]
    if not customers:
        raise SystemExit("No seeded customers; run scripts/seed_commerce.py first.")

    buckets: dict[str, list[tuple]] = {name: [] for name, _ in _TABLES}
    total_units = 0
    # Spread across the last 26 days so every order sits inside the agent's 30-day
    # window with room to spare, and no single order carries the rate on its own.
    for index, quantity in enumerate(ORDER_QUANTITIES):
        suffix = f"{index:03d}"
        customer_id = customers[index % len(customers)]
        moment = as_of - timedelta(days=25 - (index * 2), hours=index % 9, minutes=index * 3)
        correlation = f"{PREFIX}corr_{suffix}"
        conversation_id = f"{PREFIX}conv_{suffix}"
        turn_id = f"{PREFIX}turn_{suffix}"
        presentation_id = f"{PREFIX}pres_{suffix}"
        stage_id = f"{PREFIX}stage_{suffix}"
        order_id = f"{PREFIX}ord_{suffix}"
        line_id = f"{order_id}_l0"
        attempt_id = f"{PREFIX}pay_{suffix}"
        total_units += quantity

        subtotal = unit * quantity
        tax = round(subtotal * 0.18)
        shipping = 0
        total = subtotal + shipping + tax
        paid_at = _iso(moment + timedelta(minutes=6))

        buckets["conversations"].append((conversation_id, customer_id, "shopping", _iso(moment)))
        buckets["turns"].append((
            turn_id, conversation_id, 0, "completed", "I need a few more of these",
            "Here it is, with what is in stock.", "shopping@1.0.0", "tools@1.0.0",
            json.dumps([]), _iso(moment), _iso(moment + timedelta(seconds=4))))
        buckets["presentations"].append((
            presentation_id, conversation_id, customer_id, "products", turn_id,
            _iso(moment), _iso(moment + timedelta(hours=1))))
        buckets["presentation_items"].append((
            f"{presentation_id}_i0", presentation_id, 0, variant_id, unit))
        buckets["checkout_stages"].append((
            stage_id, f"{PREFIX}cart_{suffix}", customer_id, 1, "confirmed", CURRENCY,
            subtotal, shipping, tax, 0, total, None, "standard", None,
            _iso(moment + timedelta(minutes=15)), _iso(moment), _iso(moment + timedelta(minutes=2))))
        buckets["checkout_stage_lines"].append((stage_id, variant_id, quantity, unit, subtotal))
        buckets["commerce_orders"].append((
            order_id, customer_id, stage_id, "paid", CURRENCY, subtotal, shipping, tax, 0,
            total, total, None, "seeded", 1, _iso(moment), paid_at, None))
        buckets["commerce_order_lines"].append((
            line_id, order_id, variant_id, quantity, unit, subtotal, None))
        buckets["payment_attempts"].append((
            attempt_id, order_id, "razorpay", f"plink_{PREFIX}{suffix}",
            f"https://rzp.io/{PREFIX}{suffix}", "succeeded", total, CURRENCY,
            json.dumps({"seeded": True}), None, _iso(moment + timedelta(minutes=5)), paid_at))
        buckets["inbox_events"].append((
            f"{PREFIX}in_{suffix}", "razorpay", f"evt_{PREFIX}{suffix}", "payment_link.paid",
            json.dumps({"amount": total, "currency": CURRENCY,
                        "reference": f"plink_{PREFIX}{suffix}"}),
            "processed", None, paid_at, _iso(moment + timedelta(minutes=6, seconds=2))))
        buckets["commerce_events"].append((
            f"{PREFIX}evt_{suffix}_c", _iso(moment), "order_created", "order", order_id,
            customer_id, total, None, "seeded", None, correlation, None))
        buckets["commerce_events"].append((
            f"{PREFIX}evt_{suffix}_p", paid_at, "order_paid", "order", order_id,
            customer_id, total, quantity, "seeded", None, correlation, None))
        buckets["evidence_records"].append((
            f"{PREFIX}ev_{suffix}_p", _iso(moment), "customer", customer_id, "shopping",
            "settle_payment_attempt", "order", order_id,
            "Verified provider outcome for this attempt", "applied", None, None,
            "shopping@1.0.0", None, "seeded", None, correlation, turn_id, None))
        fulfillment_id = f"{PREFIX}ful_{suffix}"
        delivered = moment + timedelta(days=3)
        buckets["fulfillments"].append((
            fulfillment_id, order_id, "delivered", "BlueDart", f"BD{PREFIX}{suffix}",
            _iso(delivered), _iso(moment + timedelta(days=1)), _iso(delivered),
            _iso(moment + timedelta(hours=6))))
        buckets["fulfillment_lines"].append((fulfillment_id, line_id, quantity))

    inserts = (
        ("conversations", "INSERT INTO conversations (id,principal_id,surface,created_at) VALUES (?,?,?,?)"),
        ("turns", "INSERT INTO turns (id,conversation_id,sequence,state,user_message,agent_message,"
                  "prompt_version,tool_contract_version,skill_versions,started_at,completed_at) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?,?)"),
        ("presentations", "INSERT INTO presentations (id,conversation_id,customer_id,kind,turn_id,"
                          "created_at,expires_at) VALUES (?,?,?,?,?,?,?)"),
        ("presentation_items", "INSERT INTO presentation_items (id,presentation_id,position,variant_id,"
                               "unit_price_minor) VALUES (?,?,?,?,?)"),
        ("checkout_stages", "INSERT INTO checkout_stages (id,cart_id,customer_id,cart_state_version,"
                            "state,currency,subtotal_minor,shipping_minor,tax_minor,discount_minor,"
                            "total_minor,promotion_id,fulfillment_option,constraints_note,expires_at,"
                            "created_at,resolved_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"),
        ("checkout_stage_lines", "INSERT INTO checkout_stage_lines (stage_id,variant_id,quantity,"
                                 "unit_price_minor,amount_minor) VALUES (?,?,?,?,?)"),
        ("commerce_orders", "INSERT INTO commerce_orders (id,customer_id,stage_id,status,currency,"
                            "subtotal_minor,shipping_minor,tax_minor,discount_minor,total_minor,"
                            "amount_paid_minor,promotion_id,origin,state_version,created_at,paid_at,"
                            "cancelled_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"),
        ("commerce_order_lines", "INSERT INTO commerce_order_lines (id,order_id,variant_id,quantity,"
                                 "unit_price_minor,amount_minor,recommendation_id) VALUES (?,?,?,?,?,?,?)"),
        ("payment_attempts", "INSERT INTO payment_attempts (id,order_id,provider,provider_reference,"
                             "provider_link_url,status,amount_minor,currency,provider_snapshot,"
                             "failure_reason,created_at,resolved_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"),
        ("inbox_events", "INSERT INTO inbox_events (id,provider,provider_event_id,event_type,payload,"
                         "status,quarantine_reason,received_at,processed_at) VALUES (?,?,?,?,?,?,?,?,?)"),
        ("commerce_events", "INSERT INTO commerce_events (id,occurred_at,event_type,subject_type,"
                            "subject_id,customer_id,amount_minor,quantity,origin,demo_run_id,"
                            "correlation_id,detail) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"),
        ("evidence_records", "INSERT INTO evidence_records (id,recorded_at,actor_type,actor_id,surface,"
                             "action,target_type,target_id,reason,outcome,policy_checks,state_ref,"
                             "prompt_version,skill_versions,data_origin,demo_run_id,correlation_id,"
                             "turn_id,tool_execution_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"),
        ("fulfillments", "INSERT INTO fulfillments (id,order_id,status,carrier,tracking_reference,"
                         "promised_at,shipped_at,delivered_at,created_at) VALUES (?,?,?,?,?,?,?,?,?)"),
        ("fulfillment_lines", "INSERT INTO fulfillment_lines (fulfillment_id,order_line_id,quantity) "
                              "VALUES (?,?,?)"),
    )
    for name, sql in inserts:
        if buckets[name]:
            store.executemany(sql, buckets[name])
    return total_units, unit


def set_stock(store: Store, variant_id: str, sellable: int, as_of: datetime) -> None:
    """Put `sellable` units at the first location and clear the rest.

    Seeded history never decremented stock in the first place — the generator sets
    levels once at receipt — so this is the same kind of figure the rest of the
    seeded world carries, not a contradiction of the movement ledger.
    """
    locations = [str(r["location_id"]) for r in store.rows(
        "SELECT location_id FROM inventory_levels WHERE variant_id = ? ORDER BY location_id",
        (variant_id,))]
    if not locations:
        raise SystemExit(f"{variant_id} has no inventory rows to adjust.")
    for index, location_id in enumerate(locations):
        store.execute(
            "UPDATE inventory_levels SET on_hand = ?, reserved = 0, updated_at = ? "
            "WHERE variant_id = ? AND location_id = ?",
            (sellable if index == 0 else 0, _iso(as_of), variant_id, location_id))


def report(store: Store, variant_id: str) -> None:
    rows = store.rows(
        "SELECT SUM(on_hand - reserved) AS sellable FROM inventory_levels WHERE variant_id = ?",
        (variant_id,))
    sellable = int(rows[0]["sellable"] or 0)
    units = int(store.rows(
        "SELECT COALESCE(SUM(l.quantity),0) AS units FROM commerce_order_lines l "
        "JOIN commerce_orders o ON o.id = l.order_id WHERE l.variant_id = ? AND o.status='paid' "
        "AND o.origin='seeded' AND o.created_at >= ?",
        (variant_id, _iso(datetime.now(UTC) - timedelta(days=30))))[0]["units"])
    rate = units / 30 if units else 0.0
    cover = round(sellable / rate, 1) if rate else None
    restock = max(0, round(rate * 21) - sellable) if rate else 0
    print(f"  {variant_id}: sellable={sellable} units_30d={units} "
          f"rate={rate:.4f}/day cover={cover} days restock_to_21d={restock} units")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--variant", default="sd_prd_cable_usbc_0_v0")
    parser.add_argument("--sellable", type=int, default=TARGET_SELLABLE)
    parser.add_argument("--sqlite", help="Run against a local SQLite file instead of Supabase.")
    parser.add_argument("--undo", action="store_true",
                        help="Remove the rows this script wrote. Stock is not restored.")
    parser.add_argument("--dry-run", action="store_true", help="Report current state and exit.")
    args = parser.parse_args()

    store = open_store(args.sqlite)
    as_of = datetime.now(UTC)

    if args.dry_run:
        print("before:")
        report(store, args.variant)
        return 0
    if args.undo:
        undo(store)
        print("removed the demand burst; stock left as it is.")
        report(store, args.variant)
        return 0

    print("before:")
    report(store, args.variant)
    undo(store)  # idempotent: a second run replaces the first rather than doubling it
    units, unit_price = build(store, args.variant, as_of)
    set_stock(store, args.variant, args.sellable, as_of)
    print(f"wrote {len(ORDER_QUANTITIES)} paid seeded orders, {units} units "
          f"at {unit_price/100:.2f} INR each")
    print("after:")
    report(store, args.variant)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
