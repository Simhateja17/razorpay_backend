"""Phase 1 acceptance: authority, one durable cart, and deterministic checkout intent.

The chat assertions run against `/chat/storefront/legacy`, the regex-routed endpoint
the storefront UI still calls while it reads the legacy flat catalogue.
`/chat/storefront` is now the Messages API loop, and the same guarantee is proven
against it in tests/test_runtime_transcripts.py.
"""

import pytest
from fastapi.testclient import TestClient

from marketplace_backend.audit import AuditTrail
from marketplace_backend.carts import ConflictError, IdempotencyLedger
from marketplace_backend.identity import AuthenticationError, IdentityService, Principal
from marketplace_backend.merchant_backend import MerchantBackend
from marketplace_backend.routing import Intent, classify
from marketplace_backend.store import Store
from marketplace_backend.storefront_backend import StorefrontBackend

from test_backend import FakeMCP, sse_message

ALICE = "11111111-1111-1111-1111-111111111111"
BOB = "22222222-2222-2222-2222-222222222222"


@pytest.fixture
def shop(tmp_path):
    store = Store(tmp_path / "authority.db")
    store.executemany("INSERT INTO products VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", [
        ("P-EL-01", "Aster Wireless Earbuds", "Electronics", "Wireless earbuds", 2499, 34,
         "4.4★ (812)", "EARBUDS", None, None, None, None, 1),
        ("P-EL-02", "Aster Charger", "Electronics", "Fast charger", 999, 5,
         None, "CHARGER", None, None, None, None, 1),
    ])
    return StorefrontBackend(store, AuditTrail(store), FakeMCP())


def client_for(monkeypatch, shop, customer_id):
    monkeypatch.setenv("CARTISAN_DB_PATH", shop.store.path)
    import api.main as api_main

    class StubNarrator:
        async def say_stream(self, system, prompt):
            return
            yield  # pragma: no cover - makes this an async generator

    monkeypatch.setattr(api_main, "shop", shop)
    monkeypatch.setattr(api_main, "narrator", StubNarrator())
    api_main.app.dependency_overrides[api_main.require_customer] = lambda: Principal(
        id=customer_id, email="shopper@example.test", role="customer")
    return TestClient(api_main.app), api_main


# ------------------------------------------------- one cart per principal


def test_one_active_cart_per_customer_regardless_of_conversation(monkeypatch, shop):
    """The same principal in two conversations reads and mutates one cart."""
    client, _ = client_for(monkeypatch, shop, ALICE)

    client.post("/chat/storefront/legacy", json={"conversation_id": "morning",
                                          "message": "Please add the Aster Wireless Earbuds to my cart"})
    client.post("/chat/storefront/legacy", json={"conversation_id": "evening",
                                          "message": "Please add the Aster Charger to my cart"})

    cart = client.get("/cart").json()
    assert {line["product_id"] for line in cart["lines"]} == {"P-EL-01", "P-EL-02"}
    assert cart["cart_id"] == shop.cart_read(ALICE)["cart_id"]


def test_visible_cart_and_agent_cart_read_agree(monkeypatch, shop):
    """Acceptance 1: the REST cart the UI renders is the row the agent turn returns."""
    client, _ = client_for(monkeypatch, shop, ALICE)

    response = client.post("/chat/storefront/legacy", json={
        "conversation_id": "c1", "message": "Please add the Aster Wireless Earbuds to my cart"})
    from_turn = sse_message(response.text)["cart"]
    from_rest = client.get("/cart").json()

    assert from_turn == from_rest == shop.cart_read(ALICE)


def test_carts_are_isolated_between_customers(monkeypatch, shop):
    client_a, api_main = client_for(monkeypatch, shop, ALICE)
    client_a.post("/cart/items", json={"product_id": "P-EL-01", "quantity": 1})

    api_main.app.dependency_overrides[api_main.require_customer] = lambda: Principal(
        id=BOB, email="bob@example.test", role="customer")
    client_b = TestClient(api_main.app)

    assert client_b.get("/cart").json()["lines"] == []
    assert shop.cart_read(ALICE)["lines"][0]["product_id"] == "P-EL-01"


def test_client_cannot_name_the_cart_owner(monkeypatch, shop):
    """A body field claiming another shopper's id is ignored, not honoured."""
    client, _ = client_for(monkeypatch, shop, ALICE)

    client.post("/cart/items", json={"product_id": "P-EL-01", "quantity": 1,
                                     "session_id": BOB, "customer_id": BOB})

    assert shop.cart_read(BOB)["lines"] == []
    assert shop.cart_read(ALICE)["lines"][0]["product_id"] == "P-EL-01"


def test_unauthenticated_cart_access_is_rejected(monkeypatch, shop):
    monkeypatch.setenv("CARTISAN_DB_PATH", shop.store.path)
    import api.main as api_main
    api_main.app.dependency_overrides.clear()
    monkeypatch.setattr(api_main, "shop", shop)

    client = TestClient(api_main.app)
    assert client.get("/cart").status_code == 401
    assert client.post("/cart/items", json={"product_id": "P-EL-01"}).status_code == 401
    assert client.post("/checkout", json={}).status_code == 401


def test_orders_are_readable_only_by_their_owner(shop):
    shop.add_to_cart(ALICE, "P-EL-01", 1)
    import asyncio
    handoff = asyncio.run(shop.checkout_handoff(ALICE, "Customer approved checkout"))

    assert shop.order_status(ALICE, handoff["order_id"]) is not None
    assert shop.order_status(BOB, handoff["order_id"]) is None


# --------------------------------------------- deterministic checkout intent


@pytest.mark.parametrize("message", [
    "complete the purchase",
    "Alright leta complete the purchase ?",
    "Okay checkout",
    "please take me to checkout",
    "let's check out",
    "I'm ready to pay",
    "place my order",
])
def test_checkout_phrases_route_to_checkout(message):
    assert classify(message) is Intent.CHECKOUT


@pytest.mark.parametrize("message", [
    "What are the checkout options?",
    "add the earbuds to my cart",
    "show me wireless earbuds",
])
def test_non_checkout_phrases_do_not_route_to_checkout(message):
    assert classify(message) is not Intent.CHECKOUT


def test_checkout_intent_never_searches_or_adds(monkeypatch, shop):
    """Acceptance 2: an explicit checkout turn performs no search and no addition."""
    client, _ = client_for(monkeypatch, shop, ALICE)
    shop.add_to_cart(ALICE, "P-EL-01", 1)
    before = shop.cart_read(ALICE)

    def fail_search(*args, **kwargs):  # pragma: no cover - asserted by not running
        raise AssertionError("checkout intent must not reach product search")

    def fail_add(*args, **kwargs):  # pragma: no cover - asserted by not running
        raise AssertionError("checkout intent must not reach cart addition")

    monkeypatch.setattr(shop, "search", fail_search)
    monkeypatch.setattr(shop, "add_best_match", fail_add)

    payload = sse_message(client.post("/chat/storefront/legacy", json={
        "conversation_id": "c1", "message": "complete the purchase"}).text)

    assert payload["products"] == []
    assert payload["stagedCheckout"]["total"] == 2499
    assert shop.cart_read(ALICE) == before  # staging mutates nothing


# ----------------------------------------- state versions and idempotency


def test_every_mutation_bumps_the_state_version(shop):
    assert shop.cart_read(ALICE)["state_version"] == 0
    assert shop.add_to_cart(ALICE, "P-EL-01", 1)["state_version"] == 1
    assert shop.update_quantity(ALICE, "P-EL-01", 2)["state_version"] == 2
    assert shop.remove_from_cart(ALICE, "P-EL-01")["state_version"] == 3


def test_stale_expected_version_is_a_conflict(shop):
    shop.add_to_cart(ALICE, "P-EL-01", 1)  # version is now 1

    with pytest.raises(ConflictError):
        shop.add_to_cart(ALICE, "P-EL-02", 1, expected_version=0)

    assert shop.add_to_cart(ALICE, "P-EL-02", 1, expected_version=1)["state_version"] == 2


def test_conflict_surfaces_as_409(monkeypatch, shop):
    client, _ = client_for(monkeypatch, shop, ALICE)
    client.post("/cart/items", json={"product_id": "P-EL-01", "quantity": 1})

    response = client.post("/cart/items", json={"product_id": "P-EL-02", "quantity": 1,
                                                "expected_version": 0})
    assert response.status_code == 409


def test_replayed_idempotency_key_applies_the_effect_once(shop):
    first = shop.add_to_cart(ALICE, "P-EL-01", 1, idempotency_key="k1")
    second = shop.add_to_cart(ALICE, "P-EL-01", 1, idempotency_key="k1")

    assert first == second
    assert shop.cart_read(ALICE)["lines"][0]["quantity"] == 1
    assert shop.cart_read(ALICE)["state_version"] == 1


def test_reused_key_with_a_different_request_is_a_conflict(shop):
    shop.add_to_cart(ALICE, "P-EL-01", 1, idempotency_key="k1")

    with pytest.raises(ConflictError):
        shop.add_to_cart(ALICE, "P-EL-02", 1, idempotency_key="k1")


def test_idempotency_keys_are_scoped_to_the_principal(shop):
    shop.add_to_cart(ALICE, "P-EL-01", 1, idempotency_key="shared")
    bob = shop.add_to_cart(BOB, "P-EL-01", 1, idempotency_key="shared")

    assert bob["customer_id"] == BOB
    assert bob["lines"][0]["product_id"] == "P-EL-01"


@pytest.mark.asyncio
async def test_replayed_checkout_creates_one_payment_link(shop):
    shop.add_to_cart(ALICE, "P-EL-01", 1)

    first = await shop.checkout_handoff(ALICE, "Customer approved checkout", idempotency_key="c1")
    second = await shop.checkout_handoff(ALICE, "Customer approved checkout", idempotency_key="c1")

    assert first["order_id"] == second["order_id"]
    assert shop.store.rows("SELECT id FROM orders") and len(shop.store.rows("SELECT id FROM orders")) == 1


def test_idempotency_fingerprint_is_order_independent():
    assert IdempotencyLedger.fingerprint({"a": 1, "b": 2}) == IdempotencyLedger.fingerprint({"b": 2, "a": 1})
    assert IdempotencyLedger.fingerprint({"a": 1}) != IdempotencyLedger.fingerprint({"a": 2})


# ------------------------------------------------------------- identity


def test_missing_or_malformed_bearer_is_rejected(tmp_path):
    service = IdentityService(Store(tmp_path / "id.db"), supabase_url="https://x", anon_key="k")

    for header in (None, "", "Basic abc", "Bearer "):
        with pytest.raises(AuthenticationError):
            service.principal(header)


def test_unconfigured_supabase_refuses_rather_than_trusting_the_token(tmp_path):
    service = IdentityService(Store(tmp_path / "id.db"), supabase_url="", anon_key="")

    with pytest.raises(AuthenticationError, match="not configured"):
        service.principal("Bearer anything")


def test_role_comes_from_app_metadata_not_the_client(tmp_path):
    service = IdentityService(Store(tmp_path / "id.db"), supabase_url="https://x", anon_key="k")

    customer = service._register({"id": ALICE, "email": "a@example.test",
                                  "user_metadata": {"cartisan_role": "merchant_operator"}})
    operator = service._register({"id": BOB, "email": "b@example.test",
                                  "app_metadata": {"cartisan_role": "merchant_operator"}})

    assert customer.role == "customer"
    assert operator.role == "merchant_operator"


def test_merchant_operator_cannot_use_customer_endpoints(monkeypatch, shop):
    monkeypatch.setenv("CARTISAN_DB_PATH", shop.store.path)
    import api.main as api_main
    api_main.app.dependency_overrides.clear()
    monkeypatch.setattr(api_main, "shop", shop)
    monkeypatch.setattr(api_main.identity, "principal", lambda auth: Principal(
        id=BOB, email="ops@example.test", role="merchant_operator"))

    client = TestClient(api_main.app)
    assert client.get("/cart", headers={"Authorization": "Bearer t"}).status_code == 403


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    import api.main as api_main
    api_main.app.dependency_overrides.clear()


def test_merchant_backend_still_builds(shop):
    """Merchant surfaces are untouched by Phase 1 and must keep working."""
    merchant = MerchantBackend(shop.store, AuditTrail(shop.store), shop)
    assert merchant.business_snapshot("m1")["currency"] == "INR"
