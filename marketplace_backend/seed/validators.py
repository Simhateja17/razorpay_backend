"""Invariant validation over whatever is in the database.

These run against generated data, scenario data and live data alike — they check
the *database*, not the generator, so a seed that quietly produced an impossible
business fails here rather than in a demo. Each validator returns the offending
rows, not just a boolean, because "42 orders are wrong" is not actionable.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..store import Store


@dataclass
class InvariantReport:
    name: str
    checked: int = 0
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def __str__(self) -> str:
        status = "ok" if self.ok else f"{len(self.problems)} problem(s)"
        return f"{self.name}: {status} (checked {self.checked})"


def _report(name: str, checked: int, problems: list[str]) -> InvariantReport:
    return InvariantReport(name=name, checked=checked, problems=problems)


def validate_totals(store: Store) -> InvariantReport:
    """Order totals must equal their own lines, and paid orders must be fully paid."""
    problems = []
    orders = store.rows(
        "SELECT o.id, o.subtotal_minor, o.shipping_minor, o.tax_minor, o.discount_minor, "
        "o.total_minor, o.amount_paid_minor, o.status, "
        "(SELECT COALESCE(SUM(l.amount_minor),0) FROM commerce_order_lines l "
        " WHERE l.order_id=o.id) AS line_total, "
        "(SELECT COUNT(*) FROM commerce_order_lines l WHERE l.order_id=o.id) AS line_count "
        "FROM commerce_orders o")
    for order in orders:
        if order["line_count"] == 0:
            problems.append(f"{order['id']}: order has no lines")
            continue
        if order["subtotal_minor"] != order["line_total"]:
            problems.append(
                f"{order['id']}: subtotal {order['subtotal_minor']} != line sum {order['line_total']}")
        expected = (order["subtotal_minor"] + order["shipping_minor"] + order["tax_minor"]
                    - order["discount_minor"])
        if order["total_minor"] != expected:
            problems.append(f"{order['id']}: total {order['total_minor']} != computed {expected}")
        if order["status"] == "paid" and order["amount_paid_minor"] != order["total_minor"]:
            problems.append(
                f"{order['id']}: paid order shows {order['amount_paid_minor']} of {order['total_minor']}")
        if order["status"] != "paid" and order["amount_paid_minor"] != 0:
            problems.append(
                f"{order['id']}: unpaid order ({order['status']}) shows a paid amount")
    # A staged total must equal its own lines too, or the preview lied.
    stages = store.rows(
        "SELECT s.id, s.subtotal_minor, s.shipping_minor, s.tax_minor, s.discount_minor, s.total_minor, "
        "(SELECT COALESCE(SUM(l.amount_minor),0) FROM checkout_stage_lines l "
        " WHERE l.stage_id=s.id) AS line_total FROM checkout_stages s")
    for stage in stages:
        if stage["subtotal_minor"] != stage["line_total"]:
            problems.append(
                f"{stage['id']}: stage subtotal {stage['subtotal_minor']} != lines {stage['line_total']}")
        expected = (stage["subtotal_minor"] + stage["shipping_minor"] + stage["tax_minor"]
                    - stage["discount_minor"])
        if stage["total_minor"] != expected:
            problems.append(f"{stage['id']}: stage total {stage['total_minor']} != computed {expected}")
    return _report("totals", len(orders) + len(stages), problems)


def validate_inventory(store: Store) -> InvariantReport:
    """`on_hand` must equal its movements, and `reserved` must equal live holds."""
    problems = []
    levels = store.rows("SELECT variant_id,location_id,on_hand,reserved FROM inventory_levels")
    movements = {
        (row["variant_id"], row["location_id"]): int(row["total"] or 0)
        for row in store.rows(
            "SELECT variant_id,location_id,SUM(delta) AS total FROM inventory_movements "
            "GROUP BY variant_id,location_id")}
    held = {
        (row["variant_id"], row["location_id"]): int(row["total"] or 0)
        for row in store.rows(
            "SELECT variant_id,location_id,SUM(quantity) AS total FROM inventory_reservations "
            "WHERE status='held' GROUP BY variant_id,location_id")}
    for level in levels:
        key = (level["variant_id"], level["location_id"])
        if level["on_hand"] != movements.get(key, 0):
            problems.append(
                f"{key}: on_hand {level['on_hand']} != movement sum {movements.get(key, 0)}")
        if level["reserved"] != held.get(key, 0):
            problems.append(
                f"{key}: reserved {level['reserved']} != held reservations {held.get(key, 0)}")
        if level["reserved"] > level["on_hand"]:
            problems.append(f"{key}: reserved exceeds on_hand")
    return _report("inventory", len(levels), problems)


def validate_payments(store: Store) -> InvariantReport:
    """A paid order needs exactly one succeeded attempt, for the right amount."""
    problems = []
    orders = store.rows(
        "SELECT o.id, o.status, o.total_minor, o.currency, "
        "(SELECT COUNT(*) FROM payment_attempts p WHERE p.order_id=o.id AND p.status='succeeded') "
        "  AS succeeded_count FROM commerce_orders o")
    for order in orders:
        if order["status"] == "paid" and order["succeeded_count"] != 1:
            problems.append(
                f"{order['id']}: paid order has {order['succeeded_count']} succeeded attempts")
        if order["status"] in {"pending_payment", "cancelled", "expired"} and order["succeeded_count"]:
            problems.append(
                f"{order['id']}: {order['status']} order has a succeeded attempt")
    mismatched = store.rows(
        "SELECT p.id, p.amount_minor, p.currency, o.total_minor, o.currency AS order_currency "
        "FROM payment_attempts p JOIN commerce_orders o ON o.id=p.order_id "
        "WHERE p.status='succeeded' AND (p.amount_minor <> o.total_minor OR p.currency <> o.currency)")
    for row in mismatched:
        problems.append(
            f"{row['id']}: succeeded attempt for {row['amount_minor']} {row['currency']} "
            f"against an order of {row['total_minor']} {row['order_currency']}")
    return _report("payments", len(orders), problems)


def validate_attribution(store: Store) -> InvariantReport:
    """Attribution links must have unbroken lineage (ADR 0019).

    An attributed order line must point at a recommendation the customer actually
    accepted, for the same variant, belonging to the same customer, presented by a
    real presentation item.

    Note what is deliberately *not* checked here: whether the order was paid. A
    line on an unpaid order may legitimately record where it came from — being
    paid is what makes it count as *revenue*, which is enforced separately in
    `validate_attributed_revenue`. Conflating the two would either lose the
    provenance of abandoned carts or overstate what the agent earned.
    """
    problems = []
    lines = store.rows(
        "SELECT l.id, l.order_id, l.variant_id, l.recommendation_id, o.status, o.customer_id, "
        "r.variant_id AS rec_variant, r.customer_id AS rec_customer, r.accepted_at, "
        "r.presentation_item_id, i.variant_id AS item_variant "
        "FROM commerce_order_lines l "
        "JOIN commerce_orders o ON o.id=l.order_id "
        "LEFT JOIN recommendations r ON r.id=l.recommendation_id "
        "LEFT JOIN presentation_items i ON i.id=r.presentation_item_id "
        "WHERE l.recommendation_id IS NOT NULL")
    for line in lines:
        if line["rec_variant"] is None:
            problems.append(f"{line['id']}: attributed to a recommendation that does not exist")
            continue
        if line["accepted_at"] is None:
            problems.append(f"{line['id']}: attributed to a recommendation nobody accepted")
        if line["rec_variant"] != line["variant_id"]:
            problems.append(
                f"{line['id']}: line is {line['variant_id']} but the recommendation was "
                f"{line['rec_variant']}")
        if line["item_variant"] is None:
            problems.append(f"{line['id']}: recommendation has no presentation item behind it")
        elif line["item_variant"] != line["variant_id"]:
            problems.append(f"{line['id']}: the presented item was not the variant purchased")
        if line["rec_customer"] != line["customer_id"]:
            problems.append(f"{line['id']}: recommendation belongs to a different customer")
    return _report("attribution", len(lines), problems)


def validate_promotion_redemptions(store: Store) -> InvariantReport:
    """A recorded redemption must be one the order actually qualified for.

    This is what campaign attribution rests on: an order is attributed to a campaign
    through the promotion it redeemed, so if the redemption is not real, neither is
    the attribution. The discount has to equal what the promotion's own rule computes
    over the lines it covers — a scoped promotion is measured against its own
    category, which is also what stops "₹500 off personal audio" from being paid out
    on a keyboard.
    """
    problems = []
    orders = store.rows(
        "SELECT o.id, o.subtotal_minor, o.discount_minor, pr.code, pr.discount_kind, "
        "pr.discount_value, pr.min_subtotal_minor, pr.category_id "
        "FROM commerce_orders o JOIN promotions pr ON pr.id = o.promotion_id")
    for order in orders:
        # Branch in Python rather than passing the scope as a nullable parameter:
        # `? IS NULL` leaves Postgres an untyped parameter it refuses to infer, and
        # SQLite accepting it is exactly how that stays hidden until a live run.
        if order["category_id"] is None:
            covered = int(store.rows(
                "SELECT COALESCE(SUM(amount_minor),0) AS n FROM commerce_order_lines "
                "WHERE order_id = ?", (order["id"],))[0]["n"])
        else:
            covered = int(store.rows(
                "SELECT COALESCE(SUM(l.amount_minor),0) AS n FROM commerce_order_lines l "
                "JOIN catalog_variants v ON v.id = l.variant_id "
                "JOIN catalog_products p ON p.id = v.product_id "
                "WHERE l.order_id = ? AND p.category_id = ?",
                (order["id"], order["category_id"]))[0]["n"])
        if covered < order["min_subtotal_minor"]:
            problems.append(
                f"{order['id']}: redeemed {order['code']} on {covered} of covered lines, "
                f"below its {order['min_subtotal_minor']} minimum")
            continue
        expected = (order["discount_value"] if order["discount_kind"] == "fixed_minor"
                    else round(covered * order["discount_value"] / 100))
        expected = min(expected, covered)
        if order["discount_minor"] != expected:
            problems.append(
                f"{order['id']}: discount is {order['discount_minor']} but {order['code']} "
                f"computes {expected}")
    unexplained = store.rows(
        "SELECT id FROM commerce_orders WHERE discount_minor > 0 AND promotion_id IS NULL")
    for order in unexplained:
        problems.append(f"{order['id']}: carries a discount no promotion explains")
    return _report("promotion redemptions", len(orders), problems)


def validate_attributed_revenue(store: Store) -> InvariantReport:
    """Agent-assisted revenue counts paid orders only, and nothing else."""
    problems = []
    counted = store.rows(
        "SELECT COALESCE(SUM(l.amount_minor),0) AS total FROM commerce_order_lines l "
        "JOIN commerce_orders o ON o.id = l.order_id "
        "JOIN recommendations r ON r.id = l.recommendation_id "
        "WHERE o.status='paid' AND r.accepted_at IS NOT NULL AND r.variant_id = l.variant_id")
    everything = store.rows(
        "SELECT COALESCE(SUM(l.amount_minor),0) AS total FROM commerce_order_lines l "
        "WHERE l.recommendation_id IS NOT NULL")
    unpaid = store.rows(
        "SELECT COUNT(*) AS n FROM commerce_order_lines l "
        "JOIN commerce_orders o ON o.id = l.order_id "
        "WHERE l.recommendation_id IS NOT NULL AND o.status <> 'paid'")
    from ..metrics import MetricsRepository
    reported = MetricsRepository(store).agent_assisted_revenue(origin=None).value
    if reported != int(counted[0]["total"]):
        problems.append(
            f"reported agent-assisted revenue {reported} != paid-order lineage "
            f"{int(counted[0]['total'])}")
    if int(unpaid[0]["n"]) and reported == int(everything[0]["total"]):
        problems.append(
            "reported revenue equals every attributed line, including unpaid orders — "
            "the paid-order filter is not being applied")
    return _report("attributed revenue", 1, problems)


def validate_lineage(store: Store) -> InvariantReport:
    """No dangling references across the tables the demo actually walks."""
    checks = (
        ("commerce_order_lines -> commerce_orders",
         "SELECT l.id FROM commerce_order_lines l LEFT JOIN commerce_orders o ON o.id=l.order_id "
         "WHERE o.id IS NULL"),
        ("commerce_orders -> checkout_stages",
         "SELECT o.id FROM commerce_orders o LEFT JOIN checkout_stages s ON s.id=o.stage_id "
         "WHERE o.stage_id IS NOT NULL AND s.id IS NULL"),
        ("payment_attempts -> commerce_orders",
         "SELECT p.id FROM payment_attempts p LEFT JOIN commerce_orders o ON o.id=p.order_id "
         "WHERE o.id IS NULL"),
        ("fulfillments -> commerce_orders",
         "SELECT f.id FROM fulfillments f LEFT JOIN commerce_orders o ON o.id=f.order_id "
         "WHERE o.id IS NULL"),
        ("refunds -> payment_attempts",
         "SELECT r.id FROM refunds r LEFT JOIN payment_attempts p ON p.id=r.payment_attempt_id "
         "WHERE r.payment_attempt_id IS NOT NULL AND p.id IS NULL"),
        ("presentation_items -> catalog_variants",
         "SELECT i.id FROM presentation_items i LEFT JOIN catalog_variants v ON v.id=i.variant_id "
         "WHERE v.id IS NULL"),
        ("recommendations -> presentation_items",
         "SELECT r.id FROM recommendations r LEFT JOIN presentation_items i "
         "ON i.id=r.presentation_item_id WHERE i.id IS NULL"),
        ("turns -> conversations",
         "SELECT t.id FROM turns t LEFT JOIN conversations c ON c.id=t.conversation_id "
         "WHERE c.id IS NULL"),
        ("inventory_levels -> catalog_variants",
         "SELECT l.variant_id FROM inventory_levels l LEFT JOIN catalog_variants v "
         "ON v.id=l.variant_id WHERE v.id IS NULL"),
        ("variant_prices -> catalog_variants",
         "SELECT p.id FROM variant_prices p LEFT JOIN catalog_variants v ON v.id=p.variant_id "
         "WHERE v.id IS NULL"),
    )
    problems = []
    for label, sql in checks:
        orphans = store.rows(sql)
        if orphans:
            problems.append(f"{label}: {len(orphans)} dangling row(s), e.g. {orphans[0]}")
    return _report("lineage", len(checks), problems)


def validate_origin_labelling(store: Store) -> InvariantReport:
    """Seeded and live records must stay distinguishable (ADR 0032)."""
    problems = []
    checked = 0
    for table, column, id_column in (
        ("catalog_products", "origin", "id"),
        ("commerce_orders", "origin", "id"),
        ("commerce_events", "origin", "id"),
    ):
        rows = store.rows(
            f"SELECT {id_column} AS row_id, {column} AS origin FROM {table} "
            f"WHERE {id_column} LIKE 'sd_%' AND {column} <> 'seeded'")
        checked += 1
        if rows:
            problems.append(
                f"{table}: {len(rows)} seeded row(s) not labelled 'seeded', e.g. {rows[0]['row_id']}")
        stray = store.rows(
            f"SELECT {id_column} AS row_id FROM {table} "
            f"WHERE {id_column} NOT LIKE 'sd_%' AND {column} = 'seeded'")
        if stray:
            problems.append(
                f"{table}: {len(stray)} row(s) labelled 'seeded' without a seed id, "
                f"e.g. {stray[0]['row_id']}")
    return _report("origin labelling", checked, problems)


def validate_all(store: Store) -> list[InvariantReport]:
    return [
        validate_totals(store),
        validate_inventory(store),
        validate_payments(store),
        validate_attribution(store),
        validate_promotion_redemptions(store),
        validate_attributed_revenue(store),
        validate_lineage(store),
        validate_origin_labelling(store),
    ]
