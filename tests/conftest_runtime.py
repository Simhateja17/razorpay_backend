"""A small, real commerce core for the Phase 4 runtime tests.

Real rows rather than a stub port: catalogue grounding and structured compatibility are
only proven if the tools read the same tables production reads. The catalogue is three
variants — a laptop and two chargers, one of which cannot drive it — which is the
smallest shape that can tell a compatibility verdict from a plausible guess.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from types import SimpleNamespace

from marketplace_backend.checkout import CheckoutRepository
from marketplace_backend.evidence import CommerceEventLog, EvidenceLedger, Inbox, Outbox
from marketplace_backend.inventory import InventoryRepository
from marketplace_backend.payments import PaymentLinkDispatcher, WebhookProcessor
from marketplace_backend.shopping import ShoppingService
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
        # Every unit on hand is explained by a movement, exactly as production
        # requires, so `InventoryRepository.reconcile` is meaningful in these tests
        # rather than failing on the fixture's own unexplained stock.
        store.execute(
            "INSERT INTO inventory_movements (id,variant_id,location_id,delta,reason,"
            "created_at) VALUES (?,?,'loc-blr',?,'receipt',datetime('now'))",
            (f"mv_seed_{variant_id}", variant_id, on_hand),
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


class FakeGateway:
    """A Razorpay stand-in that behaves like the real one where it matters: the same
    `reference_id` always returns the same link, because the provider treats the
    internal order id as an idempotency key (ADR 0011)."""

    def __init__(self, fail_times: int = 0) -> None:
        self.calls: list[dict] = []
        self.fail_times = fail_times
        self._links: dict[str, dict] = {}

    async def create_payment_link(self, *, amount: int, reference_id: str, description: str) -> dict:
        self.calls.append({"amount": amount, "reference_id": reference_id})
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("provider unavailable")
        if reference_id not in self._links:
            index = len(self._links) + 1
            self._links[reference_id] = {
                "id": f"plink_test_{index}",
                "short_url": f"https://rzp.io/test/{index}",
                "amount": amount,
                "currency": "INR",
            }
        return self._links[reference_id]


def build_shopping(store: Store, gateway: FakeGateway | None = None,
                   config: CartisanAgentConfig | None = None):
    """The whole Phase 5 host surface over one store, wired as `api.main` wires it."""
    config = config or CartisanAgentConfig()
    ledger = EvidenceLedger(store)
    outbox, inbox = Outbox(store), Inbox(store)
    checkout = CheckoutRepository(
        store, InventoryRepository(store), ledger, outbox, CommerceEventLog(store)
    )
    port = CoreCommercePort(store, checkout=checkout, config=config)
    gateway = gateway or FakeGateway()
    dispatcher = PaymentLinkDispatcher(store, checkout, outbox, gateway, ledger)
    return SimpleNamespace(
        store=store, port=port, checkout=checkout, inventory=checkout.inventory,
        ledger=ledger, outbox=outbox, inbox=inbox, gateway=gateway, dispatcher=dispatcher,
        service=ShoppingService(store, port, checkout, dispatcher),
        webhooks=WebhookProcessor(store, checkout, inbox, ledger),
    )


def signed_event(event: dict, secret: str) -> tuple[bytes, str]:
    """The exact bytes a signed delivery carries, and their signature."""
    raw = json.dumps(event).encode()
    return raw, hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def paid_event(provider_reference: str, amount_minor: int, *, currency: str = "INR",
               event_id: str = "evt_1", event: str = "payment_link.paid") -> dict:
    return {
        "id": event_id,
        "event": event,
        "payload": {"payment_link": {"entity": {
            "id": provider_reference, "amount": amount_minor, "currency": currency,
            "status": "paid" if event == "payment_link.paid" else "failed"}}},
    }


def session(conversation_id: str = "conv-1") -> SessionContext:
    return SessionContext(conversation_id=conversation_id, customer_id=CUSTOMER)


def state() -> SessionState:
    return SessionState()


def turn_store(store: Store) -> TurnStore:
    return TurnStore(store)
