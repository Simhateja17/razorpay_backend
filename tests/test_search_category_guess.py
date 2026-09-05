"""Regression coverage for a category label that doesn't exist in the taxonomy.

The shopping agent guesses category names from the conversation, not from a list —
it has seen "Personal Audio" in an earlier search result and might just as easily
reach for "Earbuds" or "Chargers". Those aren't real categories, and an exact-match
filter used to turn that guess into a false empty result: the free-text query term
found the item, but the wrong category label silently excluded it anyway."""

from __future__ import annotations

from cartisan_agent.types import SearchFilters
from tests.conftest_runtime import CUSTOMER, build_shopping, build_store


async def test_unmatched_category_label_falls_back_to_free_text(tmp_path) -> None:
    store = build_store(tmp_path)
    services = build_shopping(store)
    session = services.service.session(CUSTOMER)

    # "Chargers" matches nothing in this fixture's taxonomy ("Computing",
    # "Power and charging"); the query term "charger" should still find the charger.
    results = await services.port.search_products(
        session, "charger", filters=SearchFilters(category="Chargers")
    )

    assert any("charger" in variant.title.lower() for variant in results)


async def test_partial_category_label_still_narrows_results(tmp_path) -> None:
    store = build_store(tmp_path)
    services = build_shopping(store)
    session = services.service.session(CUSTOMER)

    # "Power" shares a word with the real "Power and charging" category, so the
    # filter should still narrow to it rather than only falling back on a total miss.
    results = await services.port.search_products(
        session, "", filters=SearchFilters(category="Power")
    )

    assert results
    assert all(variant.category == "Power and charging" for variant in results)
