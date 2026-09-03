import hashlib, hmac, json, re
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from marketplace_backend.audit import AuditTrail
from marketplace_backend.identity import Principal
from marketplace_backend.merchant_backend import MerchantBackend
from marketplace_backend.store import Store
from marketplace_backend.storefront_backend import StorefrontBackend


def sse_message(text: str) -> dict:
    """Pull the final `message` event's payload out of an SSE response body that
    may also contain preceding `text_delta` events."""
    match = re.search(r"event: message\ndata: (.+)\n", text)
    assert match, f"no message event in SSE body: {text!r}"
    return json.loads(match.group(1))


def authed_client(api_main, customer_id: str) -> TestClient:
    """A client whose requests carry a verified principal.

    Identity is a dependency, never a request field, so tests substitute the
    verified principal exactly where the real Supabase check would produce it.
    """
    api_main.app.dependency_overrides[api_main.require_customer] = lambda: Principal(
        id=customer_id, email=f"{customer_id}@example.test", role="customer")
    return TestClient(api_main.app)


class FakeMCP:
    async def create_payment_link(self, **kwargs):
        assert kwargs["amount"] == 249900
        return {"id":"plink_test","short_url":"https://rzp.io/test"}


@pytest.fixture
def services(tmp_path):
    store=Store(tmp_path/"test.db")
    rows = [
        ("P-EL-01","Aster Wireless Earbuds","Electronics","Wireless earbuds",2499,34,"4.4★ (812)","EARBUDS",None,None,None,None,1),
        ("P-HK-01","Solace Coffee Maker","Home & Kitchen","Coffee maker",3199,18,None,"COFFEE",None,None,None,None,1),
        ("P-HK-02","Solace Filters","Home & Kitchen","Coffee filters",249,12,None,"FILTERS","P-HK-01",None,None,None,1),
        ("P-FA-01","Meridian Running Shoes","Fashion","Running shoes",2999,0,None,"SHOES",None,None,'{"size":["UK 8"]}',None,1),
        ("P-FA-01-8","Meridian Running Shoes — UK 8","Fashion","Running shoes",2999,8,None,"SHOES",None,"P-FA-01",None,'{"size":"UK 8"}',1),
        ("P-FA-03","Aldervale Cotton Tee","Fashion","Cotton T-shirt",799,31,None,"TEE",None,None,None,None,1),
        ("P-OOS","Nimbus Dead Stock","Electronics","Discontinued item",599,0,None,"DEAD",None,None,None,None,1),
    ]
    store.executemany("INSERT INTO products VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    audit=AuditTrail(store)
    shop=StorefrontBackend(store,audit,FakeMCP())
    return store,audit,shop,MerchantBackend(store,audit,shop)


def test_catalog_search_and_cart_are_bounded(services):
    _,audit,shop,_=services
    found=shop.search("s1","wireless earbuds")
    assert found[0]["id"] == "P-EL-01"
    cart=shop.add_to_cart("s1","P-EL-01",99)
    assert cart["lines"][0]["quantity"] == 10
    assert len(audit.list(agent="shopping")) == 2


def test_variant_family_cannot_be_carted(services):
    *_,shop,_=services
    with pytest.raises(ValueError,match="variant"):
        shop.add_to_cart("s1","P-FA-01",1)
    assert shop.add_to_cart("s1","P-FA-01-8",1)["total"] == 2999


def test_cross_sell_is_explicit_bounded_and_single_candidate(services):
    _,audit,shop,_=services
    shop.add_to_cart("upsell","P-HK-01",1)
    suggestion=shop.cross_sell("upsell","P-HK-01","Explicit filter pairing",excluded_ids={"P-HK-01"})
    assert suggestion["id"] == "P-HK-02"
    assert suggestion["cross_sell_of"] == "P-HK-01"
    assert suggestion["price"] <= shop.cart_read("upsell")["total"] * .35
    assert shop.cross_sell("upsell","P-HK-01","Duplicate excluded",excluded_ids={"P-HK-02"}) is None
    assert len([x for x in audit.list() if x["action"] == "propose_upsell"]) == 1


@pytest.mark.asyncio
async def test_checkout_url_comes_from_mcp_after_cart(services):
    _,_,shop,_=services
    shop.add_to_cart("s1","P-EL-01",1)
    handoff=await shop.checkout_handoff("s1","Customer approved checkout")
    assert handoff["pay_url"] == "https://rzp.io/test"
    assert handoff["payment_link_id"] == "plink_test"


def test_merchant_write_requires_explicit_approval(services):
    _,_,shop,merchant=services
    old=shop.products["P-EL-01"]["price"]
    proposal=merchant.propose("m1","price_update","P-EL-01",{"price":old},{"price":2199},"Slow sales")
    assert shop.products["P-EL-01"]["price"] == old
    merchant.decide("m1",proposal["id"],"approved")
    assert shop.products["P-EL-01"]["price"] == 2199
    with pytest.raises(ValueError,match="not pending"):
        merchant.decide("m1",proposal["id"],"approved")


def test_model_proposal_is_revalidated_by_code(services):
    _,_,shop,merchant=services
    kind,target,before,after,reason=merchant.validate_chat_proposal({
        "kind":"price_update","target_id":"P-FA-03","after":{"price":719},"reasoning":"Slow sell-through"})
    assert (kind,target,before,after) == ("price_update","P-FA-03",{"price":799},{"price":719})
    with pytest.raises(ValueError,match="bound"):
        merchant.validate_chat_proposal({"kind":"price_update","target_id":"P-FA-03",
                                         "after":{"price":100},"reasoning":"Too large"})
    assert shop.products["P-FA-03"]["price"] == 799


def test_webhook_rejects_bad_signature(monkeypatch,tmp_path):
    monkeypatch.setenv("CARTISAN_DB_PATH",str(tmp_path/"api.db"))
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET","testsecret")
    from api.main import app
    client=TestClient(app)
    assert client.get("/health").json() == {"status":"ok"}
    assert client.post("/webhook/razorpay",content=b"{}",headers={"X-Razorpay-Signature":"bad"}).status_code == 401


def test_storefront_chat_adds_requested_product(monkeypatch, services):
    store, _, shop, _ = services
    monkeypatch.setenv("CARTISAN_DB_PATH", store.path)
    import api.main as api_main

    class StubNarrator:
        async def say_stream(self, system, prompt):
            return
            yield  # pragma: no cover - makes this an async generator

    monkeypatch.setattr(api_main, "shop", shop)
    monkeypatch.setattr(api_main, "narrator", StubNarrator())
    client = authed_client(api_main, "chat-add")

    response = client.post("/chat/storefront", json={
        "message": "Please add the Aster Wireless Earbuds to my cart",
    })

    assert response.status_code == 200
    cart = shop.cart_read("chat-add")
    assert cart["lines"][0]["product_id"] == "P-EL-01"


def test_storefront_chat_keeps_narration_in_inr(monkeypatch, services):
    store, _, shop, _ = services
    monkeypatch.setenv("CARTISAN_DB_PATH", store.path)
    import api.main as api_main

    class StubNarrator:
        async def say_stream(self, system, prompt):
            for chunk in ("Aster Wireless Earbuds ", "cost $2,499."):
                yield chunk

    monkeypatch.setattr(api_main, "shop", shop)
    monkeypatch.setattr(api_main, "narrator", StubNarrator())
    client = authed_client(api_main, "currency-check")

    response = client.post("/chat/storefront", json={
        "message": "Show me wireless earbuds",
    })
    payload = sse_message(response.text)

    assert "₹2,499" in payload["text"]
    assert "$2,499" not in payload["text"]


def test_storefront_chat_adds_from_previous_search(monkeypatch, services):
    store, _, shop, _ = services
    monkeypatch.setenv("CARTISAN_DB_PATH", store.path)
    import api.main as api_main

    class StubNarrator:
        async def say_stream(self, system, prompt):
            return
            yield  # pragma: no cover - makes this an async generator

    monkeypatch.setattr(api_main, "shop", shop)
    monkeypatch.setattr(api_main, "narrator", StubNarrator())
    client = authed_client(api_main, "follow-up-add")

    first = client.post("/chat/storefront", json={
        "message": "Show me wireless earbuds",
    })
    assert first.status_code == 200

    second = client.post("/chat/storefront", json={
        "message": "Can you add one of them into cart",
    })
    assert second.status_code == 200
    assert shop.cart_read("follow-up-add")["lines"][0]["product_id"] == "P-EL-01"


def test_storefront_chat_checkout_phrase_stages_without_adding(monkeypatch, services):
    store, _, shop, _ = services
    monkeypatch.setenv("CARTISAN_DB_PATH", store.path)
    import api.main as api_main

    monkeypatch.setattr(api_main, "shop", shop)
    shop.add_to_cart("checkout-chat", "P-EL-01", 1)
    client = authed_client(api_main, "checkout-chat")

    response = client.post("/chat/storefront", json={
        "message": "Alright leta complete the purchase ?",
    })

    assert response.status_code == 200
    payload = sse_message(response.text)
    assert payload["stagedCheckout"]["total"] == 2499
    assert payload["products"] == []
    assert shop.cart_read("checkout-chat")["lines"][0]["product_id"] == "P-EL-01"
    assert shop.is_checkout_request("Alright leta complete the purchase ?")
    assert not shop.is_add_request("Alright leta complete the purchase ?")
    assert not shop.is_checkout_request("What are the checkout options?")


def test_storefront_chat_acknowledged_checkout_uses_authoritative_cart(monkeypatch, services):
    store, _, shop, _ = services
    monkeypatch.setenv("CARTISAN_DB_PATH", store.path)
    import api.main as api_main

    monkeypatch.setattr(api_main, "shop", shop)
    shop.add_to_cart("ack-checkout-chat", "P-EL-01", 1)
    client = authed_client(api_main, "ack-checkout-chat")

    response = client.post("/chat/storefront", json={
        "message": "Okay checkout",
    })

    assert response.status_code == 200
    payload = sse_message(response.text)
    assert payload["stagedCheckout"]["total"] == 2499
    assert payload["products"] == []
    assert payload["cart"]["lines"][0]["product_id"] == "P-EL-01"
    assert shop.is_checkout_request("Okay checkout")
    assert not shop.is_checkout_request("Okay, what are the checkout options?")


def test_empty_cart_checkout_request_does_not_add_from_search(monkeypatch, services):
    store, _, shop, _ = services
    monkeypatch.setenv("CARTISAN_DB_PATH", store.path)
    import api.main as api_main

    monkeypatch.setattr(api_main, "shop", shop)
    client = authed_client(api_main, "empty-checkout-chat")

    response = client.post("/chat/storefront", json={
        "message": "Please take me to checkout",
    })

    assert response.status_code == 200
    payload = sse_message(response.text)
    assert payload["stagedCheckout"] is None
    assert "Cart is empty" in payload["text"]
    assert shop.cart_read("empty-checkout-chat")["lines"] == []


def test_storefront_chat_does_not_add_stale_result_for_unknown_item(monkeypatch, services):
    store, _, shop, _ = services
    monkeypatch.setenv("CARTISAN_DB_PATH", store.path)
    import api.main as api_main

    class StubNarrator:
        async def say_stream(self, system, prompt):
            return
            yield  # pragma: no cover - makes this an async generator

    monkeypatch.setattr(api_main, "shop", shop)
    monkeypatch.setattr(api_main, "narrator", StubNarrator())
    client = authed_client(api_main, "stale-add")

    client.post("/chat/storefront", json={
        "message": "Show me wireless earbuds",
    })
    response = client.post("/chat/storefront", json={
        "message": "Please add Nimbus Dead Stock to my cart",
    })

    assert response.status_code == 200
    assert shop.cart_read("stale-add")["lines"] == []


@pytest.mark.asyncio
async def test_checkout_clears_persisted_cart(services):
    _, _, shop, _ = services
    shop.add_to_cart("checkout-clear", "P-EL-01", 1)

    await shop.checkout_handoff("checkout-clear", "Customer approved checkout")

    assert shop.cart_read("checkout-clear")["lines"] == []


def test_search_excludes_out_of_stock_products(services):
    _, _, shop, _ = services

    assert shop.search("search-oos", "Nimbus Dead Stock") == []


def test_checkout_bound_returns_bad_request(monkeypatch, services):
    store, _, shop, _ = services
    monkeypatch.setenv("CARTISAN_DB_PATH", store.path)
    import api.main as api_main

    monkeypatch.setattr(api_main, "shop", shop)
    shop.add_to_cart("over-bound", "P-EL-01", 10)
    client = authed_client(api_main, "over-bound")

    response = client.post("/checkout", json={"reasoning": "Customer requested checkout"})

    assert response.status_code == 400
    assert "₹10,000" in response.json()["detail"]


@pytest.mark.asyncio
async def test_merchant_turn_uses_strict_object_schema(monkeypatch):
    from marketplace_backend.agent_service import AgentNarrator

    narrator = AgentNarrator()
    captured = {}

    async def create(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps({
            "reply": "No change recommended.",
            "proposal": None,
        }))])

    monkeypatch.setattr(narrator.client.messages, "create", create)

    result = await narrator.merchant_turn("Verified snapshot: {}", "fallback")

    assert result["reply"] == "No change recommended."
    schema = captured["output_config"]["format"]["schema"]
    proposal_schema = schema["properties"]["proposal"]["anyOf"][1]
    assert proposal_schema["properties"]["after"]["additionalProperties"] is False
