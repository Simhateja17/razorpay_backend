import hashlib, hmac, json

import pytest
from fastapi.testclient import TestClient

from marketplace_backend.audit import AuditTrail
from marketplace_backend.merchant_backend import MerchantBackend
from marketplace_backend.store import Store
from marketplace_backend.storefront_backend import StorefrontBackend


class FakeMCP:
    async def create_payment_link(self, **kwargs):
        assert kwargs["amount"] == 249900
        return {"id":"plink_test","short_url":"https://rzp.io/test"}


@pytest.fixture
def services(tmp_path):
    store=Store(tmp_path/"test.db"); audit=AuditTrail(store)
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
