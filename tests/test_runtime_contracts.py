"""Phase 4 acceptance, part one: the tool contracts.

The architecture document's "Tool boundary" section is the contract, and these tests are
what makes it one. They pin the names on both surfaces, prove that every forbidden
capability is absent from the tool list *and* refused by the executor if something calls
it anyway, and prove the two schema-level rules the boundary depends on: a cart addition
names a presentation reference, and `stage_checkout` takes no cart.
"""

from __future__ import annotations

import json

import pytest

from cartisan_agent import (
    FORBIDDEN_TOOLS,
    MERCHANT_PRESENTATION,
    MERCHANT_READS,
    MERCHANT_STAGING,
    SHOPPING_MUTATIONS,
    SHOPPING_PRESENTATION,
    SHOPPING_READS,
    CartisanAgentConfig,
    CartisanToolExecutor,
    MerchantAgentConfig,
    Outcome,
    build_merchant_tools,
    build_shopping_tools,
    tool_names,
)
from cartisan_agent.contracts import build_shopping_tools as build
from cartisan_agent.gates import CHECKOUT_PRECEDENCE_GATE, FORBIDDEN_GATE
from cartisan_agent.outcomes import classify
from cartisan_agent.prompts import build_static_system
from commerce_common.skills import SkillRegistry
from tests.conftest_runtime import build_services, build_store, session, state

SKILL_NAMES = ["checkout-and-payment", "compatibility-check"]


@pytest.fixture
def executor(tmp_path):
    store = build_store(tmp_path)
    config = CartisanAgentConfig()
    return CartisanToolExecutor(
        backend=build_services(store, config),
        config=config,
        skills=SkillRegistry([]),
        session=session(),
        state=state(),
    )


# -- the boundary -------------------------------------------------------------------


def test_shopping_surface_is_exactly_the_documented_boundary():
    names = set(tool_names(build_shopping_tools(CartisanAgentConfig(), SKILL_NAMES)))
    assert names == {*SHOPPING_READS, *SHOPPING_MUTATIONS, *SHOPPING_PRESENTATION}


def test_merchant_surface_is_exactly_the_documented_boundary():
    names = set(tool_names(build_merchant_tools(MerchantAgentConfig(), SKILL_NAMES)))
    assert names == {*MERCHANT_READS, *MERCHANT_STAGING, *MERCHANT_PRESENTATION}


def test_no_surface_offers_a_forbidden_capability():
    for tools in (
        build_shopping_tools(CartisanAgentConfig(), SKILL_NAMES),
        build_merchant_tools(MerchantAgentConfig(), SKILL_NAMES),
    ):
        assert not FORBIDDEN_TOOLS & set(tool_names(tools))


@pytest.mark.parametrize("name", sorted(FORBIDDEN_TOOLS))
async def test_a_forbidden_capability_is_refused_not_merely_absent(executor, name):
    """Absence from the tool list is the first line; it is not the only one. A call
    arriving by any path — a replayed transcript, a stale client, a future surface — is
    refused in its own words, so the model never reads it as an outage to retry."""
    outcome = await executor.execute(name, {})
    assert outcome.blocked == FORBIDDEN_GATE
    assert classify(outcome) is Outcome.BLOCKED
    assert "not a capability you have" in outcome.result_text


async def test_an_unknown_tool_is_an_error_not_a_crash(executor):
    outcome = await executor.execute("teleport_order", {})
    assert outcome.is_error and "Unknown tool" in outcome.result_text


# -- the two schema rules the boundary rests on -------------------------------------


def _schema(tools, name):
    return next(tool for tool in tools if tool["name"] == name)["input_schema"]


def test_add_to_cart_takes_a_presentation_reference_and_no_catalogue_id():
    """ADR 0020 in the schema: the model cannot name an unpresented product, because
    there is no field to name one in."""
    schema = _schema(build_shopping_tools(CartisanAgentConfig(), SKILL_NAMES), "add_to_cart")
    assert schema["required"] == ["item_ref"]
    assert "variant_id" not in schema["properties"]
    assert "product_id" not in schema["properties"]


def test_stage_checkout_takes_no_cart_items_or_totals():
    """ADR 0021 in the schema: the cart is read server-side, so neither the model nor
    the transcript can substitute one."""
    schema = _schema(build_shopping_tools(CartisanAgentConfig(), SKILL_NAMES), "stage_checkout")
    assert set(schema["properties"]) <= {"status", "fulfillment_option", "note"}
    assert "required" not in schema


def test_every_read_and_write_carries_a_status_line_and_no_presentation_tool_does():
    tools = {tool["name"]: tool for tool in build_shopping_tools(CartisanAgentConfig(), SKILL_NAMES)}
    for name in (*SHOPPING_READS, *SHOPPING_MUTATIONS):
        assert "status" in tools[name]["input_schema"]["properties"], name
    for name in SHOPPING_PRESENTATION:
        assert "status" not in tools[name]["input_schema"]["properties"], name


def test_cart_quantity_ceilings_match_the_database_constraint():
    """The schema refuses what `cart_lines.quantity` would refuse, so the model gets a
    usable message instead of a failed write."""
    config = CartisanAgentConfig()
    tools = build_shopping_tools(config, SKILL_NAMES)
    for name in ("add_to_cart", "update_cart_item"):
        assert _schema(tools, name)["properties"]["quantity"]["maximum"] == config.max_quantity_per_item


# -- the cached prefix ---------------------------------------------------------------


def test_the_tool_list_and_prompt_are_the_same_bytes_on_every_request():
    """ADR 0028: the prefix is cacheable only if nothing per-request reaches it. Two
    builds from the same config must be byte-identical."""
    config = CartisanAgentConfig()
    skills = SkillRegistry([])
    first = json.dumps(build(config, SKILL_NAMES), sort_keys=True)
    second = json.dumps(build(config, SKILL_NAMES), sort_keys=True)
    assert first == second
    assert build_static_system(config, skills) == build_static_system(config, skills)


def test_switching_a_system_off_removes_its_tools_from_the_surface():
    names = set(tool_names(build_shopping_tools(CartisanAgentConfig(enable_cart=False), [])))
    assert not names & {"add_to_cart", "stage_checkout", "present_cart", "present_checkout"}
    assert "search_products" in names


@pytest.mark.parametrize(
    "name,arguments",
    [
        ("search_products", {"query": "charger"}),
        ("add_to_cart", {"item_ref": "item_whatever"}),
        ("update_cart_item", {"variant_id": "sd_var_laptop1", "quantity": 2}),
        ("remove_from_cart", {"variant_id": "sd_var_laptop1"}),
    ],
)
async def test_a_checkout_turn_refuses_search_and_cart_mutation(executor, name, arguments):
    """The gate half of checkout precedence; the loop's forced first round is the other
    half, exercised in the transcript tests. The refusal is `blocked`, not `failed`: the
    call was well-formed and the turn simply is not the turn for it."""
    executor._state.checkout_turn = True
    outcome = await executor.execute(name, arguments)
    assert outcome.blocked == CHECKOUT_PRECEDENCE_GATE
    assert classify(outcome) is Outcome.BLOCKED
    assert "stage_checkout" in outcome.result_text


async def test_the_same_calls_are_allowed_on_an_ordinary_turn(executor):
    outcome = await executor.execute("search_products", {"query": "charger"})
    assert not outcome.refused
