"""The legacy flat-catalogue layer that the merchant surfaces still sit on.

Phase 5 moved shopping — the catalogue, the cart, checkout and orders — onto the
normalized commerce core, and deleted `/chat/storefront/legacy` together with the
tests that drove it. What is left here is the part `MerchantBackend` still reads:
the flat `products` table, its search and its cross-sell bound. Phase 6 migrates
that too, and this file retires with it.

Shopping behaviour now lives in tests/test_phase5_checkout.py (the purchase and its
failure modes), tests/test_authority.py (one cart, one owner), and
tests/test_runtime_* (the agent turn).
"""

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
    shop=StorefrontBackend(store,audit)
    return store,audit,shop,MerchantBackend(store,audit,shop)


def test_search_is_bounded_and_audited(services):
    _,audit,shop,_=services
    found=shop.search("s1","wireless earbuds")
    assert found[0]["id"] == "P-EL-01"
    assert len(audit.list(agent="shopping")) == 1


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


def test_search_excludes_out_of_stock_products(services):
    _, _, shop, _ = services

    assert shop.search("search-oos", "Nimbus Dead Stock") == []


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
