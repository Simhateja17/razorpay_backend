"""The gates a tool call passes before it reaches the commerce port.

Three of them, each answering one acceptance criterion of this phase:

* **Checkout precedence** (ADR 0021). Explicit checkout language routes to
  `stage_checkout` deterministically, in code, before the model is asked anything. On
  such a turn the loop pins the first round to `stage_checkout` and this module refuses
  `search_products` and `add_to_cart` outright, so "complete the purchase" cannot
  become a search or a cart addition however the model reads it.
* **Presentation references** (ADR 0020). A cart addition names an `item_ref` the
  server issued; `presentations.PresentationLedger` resolves it or refuses.
* **Grounding** (the catalogue rule). A turn that mentions an unread catalogue id
  starts from `get_product_details`, and a turn about the store's terms starts from
  `search_policies`.

Cart writes for one conversation are serialized: a turn's calls run concurrently, and
two mutations reading, computing, and writing the same cart must not interleave.
"""

from __future__ import annotations

import asyncio
import weakref
from typing import Any

from commerce_common.grounding import GroundingRule, find_token, matches_terms_and_cues
from marketplace_backend.routing import is_checkout_request

from .config import CartisanAgentConfig
from .types import SessionContext, SessionState

CHECKOUT_PRECEDENCE_GATE = "checkout_precedence"
FORBIDDEN_GATE = "forbidden_capability"

# What an explicit checkout turn may still do. Reading the cart and the order it will
# become is part of staging; searching the catalogue or adding to the cart is not.
CHECKOUT_TURN_REFUSED: frozenset[str] = frozenset(
    {"search_products", "add_to_cart", "update_cart_item", "remove_from_cart"}
)


def is_checkout_turn(config: CartisanAgentConfig, text: str) -> bool:
    """Whether this message routes to checkout with precedence. The classifier is
    `marketplace_backend.routing`, unchanged from Phase 1: the deterministic route the
    regex host used is kept, and what changes in this phase is that it now steers a
    model loop instead of replacing one."""
    return bool(config.checkout_precedence and is_checkout_request(text))


def checkout_precedence_error(name: str) -> str:
    return (
        f"{name} is not available on this turn: the customer asked to check out, so this "
        "turn stages their authoritative cart and nothing else. Call stage_checkout, "
        "present the preview, and say what they should check before confirming. If they "
        "want to change the cart first, they will say so."
    )


def forbidden_error(name: str) -> str:
    return (
        f"{name} is not a capability you have. Creating a payment link, capturing or "
        "marking payment, releasing stock, changing a price, and approving a staged "
        "change are done by Cartisan and Razorpay, never by you. Say what you can do "
        "instead."
    )


# -- grounding rules, in precedence order ------------------------------------------


def _policy(config: CartisanAgentConfig, text: str, _: SessionState) -> dict[str, Any] | None:
    fires = config.policy_grounding_gate and matches_terms_and_cues(
        text, config.policy_intent_terms, config.policy_intent_cues
    )
    return {} if fires else None


def _orders(config: CartisanAgentConfig, text: str, _: SessionState) -> dict[str, Any] | None:
    fires = (
        config.enable_orders
        and config.order_grounding_gate
        and matches_terms_and_cues(text, config.order_intent_terms, config.order_intent_cues)
    )
    return {} if fires else None


def _catalog(config: CartisanAgentConfig, text: str, state: SessionState) -> dict[str, Any] | None:
    if not config.catalog_grounding_gate:
        return None
    token = find_token(text, config.variant_id_patterns)
    # An id this session already read needs no forced re-read.
    if token is None or token in state.seen_variants:
        return None
    return {"variant_id": token}


def _catalog_browse(
    config: CartisanAgentConfig, text: str, _: SessionState
) -> dict[str, Any] | None:
    """A store-range claim starts from the real catalogue, not the store's label."""
    fires = config.catalog_grounding_gate and matches_terms_and_cues(
        text, config.catalog_browse_terms, config.catalog_browse_cues
    )
    return {"query": ""} if fires else None


GROUNDING_RULES: tuple[GroundingRule, ...] = (
    GroundingRule("policy", "search_policies", _policy),
    GroundingRule("orders", "get_orders", _orders),
    GroundingRule("catalog_browse", "search_products", _catalog_browse),
    GroundingRule("catalog", "get_product_details", _catalog),
)


# -- per-conversation serialization -------------------------------------------------

_cart_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()


def cart_lock(session: SessionContext) -> asyncio.Lock:
    """The lock a cart write holds while it reads, computes, and writes. Held only for
    the life of the write, and keyed by conversation so different customers and
    different conversations stay concurrent (ADR 0029)."""
    lock = _cart_locks.get(session.conversation_id)
    if lock is None:
        lock = _cart_locks[session.conversation_id] = asyncio.Lock()
    return lock
