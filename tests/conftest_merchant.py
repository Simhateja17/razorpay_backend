"""A small, real merchant world for the Phase 6 tests.

Real rows again, for the same reason the shopping fixture uses them: a metric is
only proven if it was derived from the same `commerce_events` production derives
it from, and a staged change is only proven if it was written to the same
`merchant_changes` table the approval surface reads.

On top of `conftest_runtime`'s three-variant catalogue this adds a trading
history: paid orders and their events over the last few weeks, one refund, one
order created and never paid, plus a promotion and a campaign. It is deliberately
small enough that every figure in a test can be worked out by hand — which is
what makes "the formula's inputs are checkable" something a test can check.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from marketplace_backend.evidence import EvidenceLedger
from marketplace_backend.merchant import MerchantService
from marketplace_backend.merchant_changes import MerchantChangeRepository
from marketplace_backend.metrics import MetricsRepository
from marketplace_backend.store import Store

from cartisan_agent import CoreMerchantPort, MerchantAgentConfig, MerchantServices
from cartisan_agent.merchant_types import MerchantSessionContext, MerchantSessionState
from conftest_runtime import GOOD_CHARGER, LAPTOP, WEAK_CHARGER, build_store

OPERATOR = "33333333-3333-3333-3333-333333333333"

# Laptop ₹8,499, good charger ₹2,499, weak charger ₹1,299 (from conftest_runtime).
LAPTOP_PRICE = 8_499_00
GOOD_PRICE = 2_499_00

# The history, written out rather than generated: (days ago, variant, quantity).
# Fourteen chargers and three laptops over the last four weeks, so a thirty-day
# window sees all of it and a seven-day window sees only the first two rows.
SALES: tuple[tuple[int, str, int], ...] = (
    (2, GOOD_CHARGER, 3),
    (5, GOOD_CHARGER, 2),
    (9, GOOD_CHARGER, 4),
    (12, LAPTOP, 1),
    (16, GOOD_CHARGER, 5),
    (20, LAPTOP, 2),
)


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def build_merchant_store(tmp_path) -> Store:
    """The shopping fixture's catalogue, plus a trading history to derive from."""
    store = build_store(tmp_path)
    store.execute(
        "INSERT INTO merchant_operators (id,email,display_name) VALUES (?,?,?)",
        (OPERATOR, "ops@example.test", "Priya"))
    now = datetime.now(UTC)
    prices = {LAPTOP: LAPTOP_PRICE, GOOD_CHARGER: GOOD_PRICE, WEAK_CHARGER: 1_299_00}

    for index, (days_ago, variant_id, quantity) in enumerate(SALES):
        moment = now - timedelta(days=days_ago)
        order_id = f"sd_ord_m{index}"
        amount = prices[variant_id] * quantity
        store.execute(
            "INSERT INTO commerce_orders (id,customer_id,status,currency,subtotal_minor,"
            "shipping_minor,tax_minor,discount_minor,total_minor,amount_paid_minor,origin,"
            "created_at,paid_at) VALUES (?,?,'paid','INR',?,0,0,0,?,?,'seeded',?,?)",
            (order_id, "sd_cust_1", amount, amount, amount, _iso(moment), _iso(moment)))
        store.execute(
            "INSERT INTO commerce_order_lines (id,order_id,variant_id,quantity,"
            "unit_price_minor,amount_minor) VALUES (?,?,?,?,?,?)",
            (f"{order_id}_l0", order_id, variant_id, quantity, prices[variant_id], amount))
        for suffix, event_type in (("c", "order_created"), ("p", "order_paid")):
            store.execute(
                "INSERT INTO commerce_events (id,occurred_at,event_type,subject_type,"
                "subject_id,customer_id,amount_minor,origin) "
                "VALUES (?,?,?,'order',?,?,?,'seeded')",
                (f"sd_evt_m{index}_{suffix}", _iso(moment), event_type, order_id,
                 "sd_cust_1", amount))

    # One order created and never paid, so conversion and abandonment are not 1.0,
    # and one completed refund, so revenue is net of something.
    abandoned = now - timedelta(days=4)
    store.execute(
        "INSERT INTO commerce_orders (id,customer_id,status,currency,subtotal_minor,"
        "shipping_minor,tax_minor,discount_minor,total_minor,amount_paid_minor,origin,"
        "created_at) VALUES ('sd_ord_open','sd_cust_2','pending_payment','INR',?,0,0,0,?,0,"
        "'seeded',?)",
        (GOOD_PRICE, GOOD_PRICE, _iso(abandoned)))
    store.execute(
        "INSERT INTO commerce_events (id,occurred_at,event_type,subject_type,subject_id,"
        "customer_id,amount_minor,origin) VALUES ('sd_evt_open_c',?,'order_created','order',"
        "'sd_ord_open','sd_cust_2',?,'seeded')",
        (_iso(abandoned), GOOD_PRICE))
    store.execute(
        "INSERT INTO commerce_events (id,occurred_at,event_type,subject_type,subject_id,"
        "customer_id,amount_minor,origin) VALUES ('sd_evt_ref',?,'order_refunded','order',"
        "'sd_ord_m3','sd_cust_1',?,'seeded')",
        (_iso(now - timedelta(days=10)), LAPTOP_PRICE))

    # Draw the charger's stock down to four units, so it is genuinely below cover at
    # the rate above and the alerts read has something real to return. The movement
    # explains the drawdown, exactly as production requires, so `reconcile` still holds.
    store.execute(
        "UPDATE inventory_levels SET on_hand=4 WHERE variant_id=?", (GOOD_CHARGER,))
    store.execute(
        "INSERT INTO inventory_movements (id,variant_id,location_id,delta,reason,created_at) "
        "VALUES ('sd_mv_sale',?,'loc-blr',-5,'sale',?)", (GOOD_CHARGER, _iso(now)))

    store.execute(
        "INSERT INTO promotions (id,code,description,discount_kind,discount_value,"
        "min_subtotal_minor,status,starts_at) VALUES ('sd_promo_x','MONSOON10',"
        "'10% off accessories','percentage',10,0,'active',?)", (_iso(now - timedelta(days=30)),))
    store.execute(
        "INSERT INTO campaigns (id,name,channel,promotion_id,status,budget_minor,"
        "spend_minor,starts_at) VALUES ('sd_camp_x','Monsoon accessories','email',"
        "'sd_promo_x','running',500000,180000,?)", (_iso(now - timedelta(days=30)),))
    # A campaign with no promotion behind it: nothing links an order to it, which is
    # the case where attribution has to read as absent rather than as zero.
    store.execute(
        "INSERT INTO campaigns (id,name,channel,promotion_id,status,budget_minor,"
        "spend_minor,starts_at) VALUES ('sd_camp_y','Brand awareness','display',"
        "NULL,'running',400000,120000,?)", (_iso(now - timedelta(days=30)),))
    return store


def build_merchant(store: Store, config: MerchantAgentConfig | None = None):
    """The whole merchant surface over one store, wired as `api.main` wires it."""
    config = config or MerchantAgentConfig()
    ledger = EvidenceLedger(store)
    changes = MerchantChangeRepository(store, ledger)
    port = CoreMerchantPort(
        store, changes=changes, metrics=MetricsRepository(store), config=config)
    return SimpleMerchantWorld(
        store=store, ledger=ledger, changes=changes, port=port, config=config,
        services=MerchantServices(port=port),
        service=MerchantService(store, port, changes, ledger),
    )


class SimpleMerchantWorld:
    def __init__(self, **fields) -> None:
        self.__dict__.update(fields)


def merchant_session(conversation_id: str = "portal-1") -> MerchantSessionContext:
    return MerchantSessionContext(conversation_id=conversation_id, customer_id=OPERATOR)


def merchant_state() -> MerchantSessionState:
    return MerchantSessionState()


def evidence_for(store: Store, action: str, target_id: str) -> list[dict]:
    """Every ledger record for one action against one target, oldest first."""
    return store.rows(
        "SELECT * FROM evidence_records WHERE action=? AND target_id=? ORDER BY recorded_at, id",
        (action, target_id))
