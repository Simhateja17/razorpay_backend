"""A small, real commerce core for the Phase 4 runtime tests.

Real rows rather than a stub port: catalogue grounding and structured compatibility are
only proven if the tools read the same tables production reads. The catalogue is three
variants — a laptop and two chargers, one of which cannot drive it — which is the
smallest shape that can tell a compatibility verdict from a plausible guess.
"""

from __future__ import annotations

from marketplace_backend.checkout import CheckoutRepository
from marketplace_backend.evidence import CommerceEventLog, EvidenceLedger, Outbox
from marketplace_backend.inventory import InventoryRepository
from marketplace_backend.store import Store

from cartisan_agent import CoreCommercePort, PresentationLedger, TurnStore
from cartisan_agent.config import CartisanAgentConfig
from cartisan_agent.executor import CommerceServices
from cartisan_agent.types import SessionContext, SessionState

CUSTOMER = "11111111-1111-1111-1111-111111111111"
LAPTOP = "sd_var_laptop1"
GOOD_CHARGER = "sd_var_charger65"
WEAK_CHARGER = "sd_var_charger30"


def build_store(tmp_path) -> Store:
    store = Store(tmp_path / "runtime.db")
    store.execute(
        "INSERT INTO customers (id,email,display_name,origin,created_at) "
        "VALUES (?,?,?,'live_app',datetime('now'))",
        (CUSTOMER, "ada@example.test", "Ada"),
    )
    store.execute("INSERT INTO catalog_categories (id,name) VALUES ('cat-computing','Computing')")
    store.execute(
        "INSERT INTO catalog_categories (id,name) VALUES ('cat-power','Power and charging')"
    )
    store.execute(
        "INSERT INTO inventory_locations (id,code,name,region) "
        "VALUES ('loc-blr','BLR','Bengaluru hub','South')"
    )
    store.execute(
        "INSERT INTO capabilities (id,label,value_kind) "
        "VALUES ('cap-pd','USB-C PD output (W)','numeric')"
    )

    def product(product_id: str, sku_root: str, title: str, brand: str, category: str) -> None:
        store.execute(
            "INSERT INTO catalog_products (id,sku_root,title,brand,category_id,description,"
            "status,origin) VALUES (?,?,?,?,?,?,'active','seeded')",
            (product_id, sku_root, title, brand, category, f"{title} by {brand}."),
        )

    def variant(
        variant_id: str, product_id: str, sku: str, title: str, price_minor: int, on_hand: int
    ) -> None:
        store.execute(
            "INSERT INTO catalog_variants (id,product_id,sku,title,options,status) "
            "VALUES (?,?,?,?,?,'active')",
            (variant_id, product_id, sku, title, "{}"),
        )
        store.execute(
            "INSERT INTO variant_prices (id,variant_id,currency,amount_minor,price_kind,valid_from) "
            "VALUES (?,?,'INR',?,'list','2020-01-01T00:00:00+00:00')",
            (f"price_{variant_id}", variant_id, price_minor),
        )
        store.execute(
            "INSERT INTO inventory_levels (variant_id,location_id,on_hand,reserved) "
            "VALUES (?,'loc-blr',?,0)",
            (variant_id, on_hand),
        )

    product("sd_prod_laptop", "LAP-1", "Aster 14 laptop", "Aster", "cat-computing")
    variant(LAPTOP, "sd_prod_laptop", "LAP-1-512", "Aster 14 laptop, 512 GB", 8_499_00, 5)
    product("sd_prod_charger", "CHG-1", "Nimbus travel charger", "Nimbus", "cat-power")
    variant(GOOD_CHARGER, "sd_prod_charger", "CHG-1-65", "Nimbus charger 65 W", 2_499_00, 9)
    variant(WEAK_CHARGER, "sd_prod_charger", "CHG-1-30", "Nimbus charger 30 W", 1_299_00, 9)

    # The laptop states what it needs; each charger states what it offers. The verdict
    # is these rows evaluated, and nothing else (ADR 0006).
    store.execute(
        "INSERT INTO variant_requirements (id,variant_id,capability_id,operator,value_numeric,"
        "severity,explanation) VALUES ('req-1',?,'cap-pd','gte',65,'blocking',?)",
        (LAPTOP, "The Aster 14 charges over USB-C PD and needs at least 65 W."),
    )
    for variant_id, watts in ((GOOD_CHARGER, 65), (WEAK_CHARGER, 30)):
        store.execute(
            "INSERT INTO variant_capabilities (variant_id,capability_id,value_numeric) "
            "VALUES (?,'cap-pd',?)",
            (variant_id, watts),
        )
    return store


def build_services(store: Store, config: CartisanAgentConfig | None = None) -> CommerceServices:
    config = config or CartisanAgentConfig()
    ledger = EvidenceLedger(store)
    checkout = CheckoutRepository(
        store, InventoryRepository(store), ledger, Outbox(store), CommerceEventLog(store)
    )
    return CommerceServices(
        port=CoreCommercePort(store, checkout=checkout, config=config),
        presentations=PresentationLedger(store, config),
    )


def session(conversation_id: str = "conv-1") -> SessionContext:
    return SessionContext(conversation_id=conversation_id, customer_id=CUSTOMER)


def state() -> SessionState:
    return SessionState()


def turn_store(store: Store) -> TurnStore:
    return TurnStore(store)
