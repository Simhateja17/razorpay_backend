"""Phase 1 acceptance, re-proven on the Phase 5 surface.

The guarantees are unchanged — one durable cart per verified principal, no client-
supplied owner, a stale version is a conflict, an order is readable only by the
person who placed it — but the cart they hold for is now the variant-keyed cart on
the normalized commerce core, which the browser and the agent share. The chat
assertions that used to run against `/chat/storefront/legacy` are gone with that
endpoint; the same behaviour is proven against the real loop in
tests/test_runtime_transcripts.py.
"""

import pytest
from fastapi.testclient import TestClient

from marketplace_backend.carts import ConflictError, IdempotencyLedger
from marketplace_backend.identity import AuthenticationError, IdentityService, Principal
from marketplace_backend.routing import Intent, classify
from marketplace_backend.store import Store

from conftest_runtime import CUSTOMER, GOOD_CHARGER, LAPTOP, build_shopping, build_store

ALICE = CUSTOMER
BOB = "22222222-2222-2222-2222-222222222222"


@pytest.fixture
def world(tmp_path):
    store = build_store(tmp_path)
    store.execute(
        "INSERT INTO customers (id,email,display_name,origin,created_at) "
        "VALUES (?,?,?,'live_app',datetime('now'))",
        (BOB, "bob@example.test", "Bob"),
    )
    return build_shopping(store)


def client_for(monkeypatch, world, customer_id):
    """A client whose requests carry a verified principal, over the test store.

    Identity is a dependency, never a request field, so the override substitutes the
    verified principal exactly where the real Supabase check would produce it.
    """
    import api.main as api_main

    monkeypatch.setattr(api_main, "db", world.store)
    monkeypatch.setattr(api_main, "shopping", world.service)
    monkeypatch.setattr(api_main, "checkout_repo", world.checkout)
    api_main.app.dependency_overrides[api_main.require_customer] = lambda: Principal(
        id=customer_id, email="shopper@example.test", role="customer")
    return TestClient(api_main.app), api_main


def cart_of(world, customer_id):
    import asyncio

    return asyncio.run(world.service.cart(customer_id))


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    import api.main as api_main
    api_main.app.dependency_overrides.clear()


# ------------------------------------------------- one cart per principal


def test_one_active_cart_per_customer_regardless_of_conversation(monkeypatch, world):
    """The same principal writing from two places reads one cart."""
    client, _ = client_for(monkeypatch, world, ALICE)

    client.post("/cart/items", json={"variant_id": LAPTOP, "quantity": 1})
    client.post("/cart/items", json={"variant_id": GOOD_CHARGER, "quantity": 1})

    cart = client.get("/cart").json()
    assert {line["variant_id"] for line in cart["lines"]} == {LAPTOP, GOOD_CHARGER}
    assert cart["cart_id"] == cart_of(world, ALICE)["cart_id"]
    assert len(world.store.rows(
        "SELECT id FROM customer_carts WHERE customer_id=? AND status='active'", (ALICE,))) == 1


def test_visible_cart_and_agent_cart_read_agree(monkeypatch, world):
    """Acceptance 1, and the Phase 4 carry-over closed: the REST cart the UI renders
    and the cart the agent's `get_cart` returns are the same row, line for line."""
    import asyncio

    client, _ = client_for(monkeypatch, world, ALICE)
    client.post("/cart/items", json={"variant_id": LAPTOP, "quantity": 2})

    from_rest = client.get("/cart").json()
    from_agent = asyncio.run(world.port.get_cart(world.service.session(ALICE)))

    assert from_rest["cart_id"] == from_agent.cart_id
    assert from_rest["state_version"] == from_agent.state_version
    assert from_rest["subtotal_minor"] == from_agent.subtotal_minor
    assert [(line["variant_id"], line["quantity"]) for line in from_rest["lines"]] == [
        (line.variant_id, line.quantity) for line in from_agent.lines]


def test_carts_are_isolated_between_customers(monkeypatch, world):
    client_a, api_main = client_for(monkeypatch, world, ALICE)
    client_a.post("/cart/items", json={"variant_id": LAPTOP, "quantity": 1})

    api_main.app.dependency_overrides[api_main.require_customer] = lambda: Principal(
        id=BOB, email="bob@example.test", role="customer")
    client_b = TestClient(api_main.app)

    assert client_b.get("/cart").json()["lines"] == []
    assert cart_of(world, ALICE)["lines"][0]["variant_id"] == LAPTOP


def test_client_cannot_name_the_cart_owner(monkeypatch, world):
    """A body field claiming another shopper's id is ignored, not honoured."""
    client, _ = client_for(monkeypatch, world, ALICE)

    client.post("/cart/items", json={"variant_id": LAPTOP, "quantity": 1,
                                     "customer_id": BOB, "session_id": BOB})

    assert cart_of(world, BOB)["lines"] == []
    assert cart_of(world, ALICE)["lines"][0]["variant_id"] == LAPTOP


def test_unauthenticated_shopping_access_is_rejected(monkeypatch, world):
    import api.main as api_main
    api_main.app.dependency_overrides.clear()
    monkeypatch.setattr(api_main, "shopping", world.service)

    client = TestClient(api_main.app)
    assert client.get("/cart").status_code == 401
    assert client.post("/cart/items", json={"variant_id": LAPTOP}).status_code == 401
    assert client.post("/checkout/stage", json={}).status_code == 401
    assert client.post("/checkout/confirm", json={"stage_id": "stage_x"}).status_code == 401
    assert client.get("/orders").status_code == 401


def test_orders_are_readable_only_by_their_owner(monkeypatch, world):
    import asyncio

    client, api_main = client_for(monkeypatch, world, ALICE)
    client.post("/cart/items", json={"variant_id": LAPTOP, "quantity": 1})
    stage = client.post("/checkout/stage", json={}).json()
    order_id = asyncio.run(
        world.service.confirm(ALICE, stage["stage_id"]))["order"]["order_id"]

    assert client.get(f"/orders/{order_id}").status_code == 200

    api_main.app.dependency_overrides[api_main.require_customer] = lambda: Principal(
        id=BOB, email="bob@example.test", role="customer")
    other = TestClient(api_main.app)
    # A 404 rather than a 403: telling Bob that Alice's order id is real is itself
    # a leak, so an order he does not own does not exist.
    assert other.get(f"/orders/{order_id}").status_code == 404
    assert other.post(f"/orders/{order_id}/payment").status_code == 404
    assert other.post(f"/orders/{order_id}/redirect").status_code == 404


def test_maintenance_endpoints_are_not_public(monkeypatch, world):
    import api.main as api_main
    api_main.app.dependency_overrides.clear()
    monkeypatch.delenv("CARTISAN_OPS_TOKEN", raising=False)

    client = TestClient(api_main.app)
    assert client.post("/admin/expire").status_code == 401
    assert client.post("/admin/payments/drain").status_code == 401

    monkeypatch.setenv("CARTISAN_OPS_TOKEN", "ops-secret")
    monkeypatch.setattr(api_main, "checkout_repo", world.checkout)
    assert client.post("/admin/expire", headers={"X-Cartisan-Ops-Token": "ops-secret"}
                       ).status_code == 200
    assert client.post("/admin/expire", headers={"X-Cartisan-Ops-Token": "wrong"}
                       ).status_code == 401


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


# ----------------------------------------- state versions and idempotency


def test_every_mutation_bumps_the_state_version(monkeypatch, world):
    client, _ = client_for(monkeypatch, world, ALICE)

    assert client.get("/cart").json()["state_version"] == 0
    assert client.post("/cart/items", json={"variant_id": LAPTOP, "quantity": 1}
                       ).json()["state_version"] == 1
    assert client.patch("/cart/items", json={"variant_id": LAPTOP, "quantity": 2}
                        ).json()["state_version"] == 2
    assert client.delete(f"/cart/items/{LAPTOP}").json()["state_version"] == 3


def test_conflict_surfaces_as_409(monkeypatch, world):
    client, _ = client_for(monkeypatch, world, ALICE)
    client.post("/cart/items", json={"variant_id": LAPTOP, "quantity": 1})

    stale = client.post("/cart/items", json={"variant_id": GOOD_CHARGER, "quantity": 1,
                                             "expected_version": 0})
    assert stale.status_code == 409

    fresh = client.post("/cart/items", json={"variant_id": GOOD_CHARGER, "quantity": 1,
                                             "expected_version": 1})
    assert fresh.status_code == 200 and fresh.json()["state_version"] == 2


def test_replayed_idempotency_key_applies_the_effect_once(monkeypatch, world):
    client, _ = client_for(monkeypatch, world, ALICE)

    first = client.post("/cart/items", json={"variant_id": LAPTOP, "quantity": 1,
                                             "idempotency_key": "k1"}).json()
    second = client.post("/cart/items", json={"variant_id": LAPTOP, "quantity": 1,
                                              "idempotency_key": "k1"}).json()

    assert first == second
    cart = client.get("/cart").json()
    assert cart["lines"][0]["quantity"] == 1 and cart["state_version"] == 1


def test_reused_key_with_a_different_request_is_a_conflict(monkeypatch, world):
    client, _ = client_for(monkeypatch, world, ALICE)
    client.post("/cart/items", json={"variant_id": LAPTOP, "quantity": 1,
                                     "idempotency_key": "k1"})

    reused = client.post("/cart/items", json={"variant_id": GOOD_CHARGER, "quantity": 1,
                                              "idempotency_key": "k1"})
    assert reused.status_code == 409


def test_idempotency_keys_are_scoped_to_the_principal(monkeypatch, world):
    client_a, api_main = client_for(monkeypatch, world, ALICE)
    client_a.post("/cart/items", json={"variant_id": LAPTOP, "quantity": 1,
                                       "idempotency_key": "shared"})

    api_main.app.dependency_overrides[api_main.require_customer] = lambda: Principal(
        id=BOB, email="bob@example.test", role="customer")
    bob = TestClient(api_main.app).post(
        "/cart/items", json={"variant_id": LAPTOP, "quantity": 1,
                             "idempotency_key": "shared"}).json()

    assert bob["customer_id"] == BOB
    assert bob["lines"][0]["variant_id"] == LAPTOP


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


def test_merchant_operator_cannot_use_customer_endpoints(monkeypatch, world):
    import api.main as api_main
    api_main.app.dependency_overrides.clear()
    monkeypatch.setattr(api_main, "shopping", world.service)
    monkeypatch.setattr(api_main.identity, "principal", lambda auth: Principal(
        id=BOB, email="ops@example.test", role="merchant_operator"))

    client = TestClient(api_main.app)
    headers = {"Authorization": "Bearer t"}
    assert client.get("/cart", headers=headers).status_code == 403
    assert client.get("/chat/storefront/conversations", headers=headers).status_code == 403


def test_customer_can_list_only_their_storefront_conversations(monkeypatch, world):
    """Chat history is scoped by the verified customer and the shopping surface."""
    client, _ = client_for(monkeypatch, world, ALICE)

    def add_conversation(conversation_id, principal_id, surface, started_at, message):
        world.store.execute(
            "INSERT INTO conversations (id,principal_id,surface,created_at) VALUES (?,?,?,?)",
            (conversation_id, principal_id, surface, started_at),
        )
        world.store.execute(
            "INSERT INTO turns (id,conversation_id,sequence,state,user_message,agent_message,"
            "prompt_version,tool_contract_version,skill_versions,started_at,completed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                f"turn_{conversation_id}", conversation_id, 0, "completed", message,
                "Grounded reply", "prompt", "tools", "[]", started_at, started_at,
            ),
        )

    add_conversation(
        f"{ALICE}:chat-old", ALICE, "shopping", "2026-09-04T10:00:00+00:00",
        "Show me chargers",
    )
    add_conversation(
        f"{ALICE}:chat-new", ALICE, "shopping", "2026-09-04T11:00:00+00:00",
        "Find a laptop",
    )
    add_conversation(
        f"{ALICE}:merchant", ALICE, "merchant", "2026-09-04T12:00:00+00:00",
        "Show the sales snapshot",
    )
    add_conversation(
        f"{BOB}:chat-private", BOB, "shopping", "2026-09-04T13:00:00+00:00",
        "Show Bob's cart",
    )

    response = client.get("/chat/storefront/conversations")

    assert response.status_code == 200
    assert response.json() == [
        {
            "conversation_id": "chat-new",
            "title": "Find a laptop",
            "turn_count": 1,
            "created_at": "2026-09-04T11:00:00+00:00",
            "updated_at": "2026-09-04T11:00:00+00:00",
        },
        {
            "conversation_id": "chat-old",
            "title": "Show me chargers",
            "turn_count": 1,
            "created_at": "2026-09-04T10:00:00+00:00",
            "updated_at": "2026-09-04T10:00:00+00:00",
        },
    ]


def test_a_customer_cannot_use_the_merchant_endpoints(monkeypatch, world):
    """The mirror of the test above. Phase 6 put the whole merchant surface behind an
    operator principal, so a signed-in shopper reaches none of it."""
    import api.main as api_main
    api_main.app.dependency_overrides.clear()
    monkeypatch.setattr(api_main.identity, "principal", lambda auth: Principal(
        id=ALICE, email="alice@example.test", role="customer"))

    client = TestClient(api_main.app)
    headers = {"Authorization": "Bearer t"}
    assert client.get("/portal/changes", headers=headers).status_code == 403
    assert client.get("/portal/snapshot", headers=headers).status_code == 403
    assert client.post("/chat/portal", headers=headers,
                       json={"conversation_id": "c", "message": "hi"}).status_code == 403


def test_conflict_error_is_still_the_cart_conflict_type():
    assert issubclass(ConflictError, Exception)
