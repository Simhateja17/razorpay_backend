"""Product browsing resolves only active catalog records and never grants cart authority."""
import pytest
from commerce_common.streaming import AgentEvent
from conftest_runtime import GOOD_CHARGER
from test_authority import world, client_for, ALICE, _clear_overrides


def test_product_details_and_unknown_variant(monkeypatch, world):
    client, _ = client_for(monkeypatch, world, ALICE)
    details = client.get(f"/catalog/variants/{GOOD_CHARGER}")
    assert details.status_code == 200
    assert details.json()["variant_id"] == GOOD_CHARGER
    assert "specs" in details.json()
    assert client.get("/catalog/variants/not-a-product").status_code == 404


def test_selected_product_context_is_server_resolved_and_clears(monkeypatch, world):
    client, main = client_for(monkeypatch, world, ALICE)
    received = []

    async def stream(messages, session, state):
        received.append((session.page.model_dump(), dict(state.issued_items)))
        yield AgentEvent.error("Test runtime: context captured")

    monkeypatch.setattr(main.shopping_agent, "stream_turn", stream)
    request = {"conversation_id": "browse-context", "message": "Is there a similar product for less?",
               "variant_id": GOOD_CHARGER, "page": {"extra": {"price_minor": 1}}}
    assert client.post("/chat/storefront", json=request).status_code == 200
    page, issued = received[-1]
    assert page["page_type"] == "product"
    assert page["variant_id"] == GOOD_CHARGER
    assert page["extra"]["product"]["price_minor"] != 1
    assert issued == {}
    request.pop("variant_id")
    assert client.post("/chat/storefront", json=request).status_code == 200
    assert received[-1][0]["page_type"] == "home"
    assert received[-1][0]["variant_id"] is None


def test_inactive_product_cannot_enter_agent_context(monkeypatch, world):
    client, _ = client_for(monkeypatch, world, ALICE)
    world.store.execute("UPDATE catalog_variants SET status = 'discontinued' WHERE id = ?", (GOOD_CHARGER,))
    assert client.get(f"/catalog/variants/{GOOD_CHARGER}").status_code == 404
    assert client.post("/chat/storefront", json={"message": "Compare this", "variant_id": GOOD_CHARGER}).status_code == 404
