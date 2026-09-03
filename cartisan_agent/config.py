"""Per-deployment settings. Fields that render into the static prompt or the tool
list are constant within a deployment, because those bytes are the cached prefix
(ADR 0028); the rest steer the loop only.
"""

from __future__ import annotations

from pydantic import Field

from commerce_common.config import BaseAgentConfig, ThinkingEffort

# Tools no Cartisan agent may ever hold, on either surface. Creating a payment link,
# marking or capturing payment, releasing stock, changing a live price, and approving
# a staged change are host and provider capabilities (ADR 0015, ADR 0016). The list is
# asserted against the built tool surfaces in the contract tests, so a name added to a
# registry by accident fails a test rather than reaching a model.
FORBIDDEN_TOOLS: frozenset[str] = frozenset(
    {
        "create_payment_link",
        "capture_payment",
        "mark_order_paid",
        "confirm_checkout",
        "refund_payment",
        "release_inventory",
        "release_reservation",
        "set_price",
        "apply_price_update",
        "apply_change",
        "approve_change",
        "reject_change",
        "delete_order",
        "save_memory",
    }
)


class CartisanAgentConfig(BaseAgentConfig):
    brand_name: str = "Cartisan"
    model: str = "claude-sonnet-5"
    thinking_effort: ThinkingEffort | None = "low"

    # -- Cart caps, enforced by the gates on every path. Ten units per line matches
    # the CHECK constraint on `cart_lines.quantity`, so the gate refuses before the
    # database would.
    max_quantity_per_item: int = Field(default=10, ge=1, le=10)
    max_cart_lines: int = Field(default=20, ge=1)

    # -- The one proactive cross-sell the agent may present (ADR 0007). Zero switches
    # cross-selling off entirely.
    max_cross_sells_per_turn: int = Field(default=1, ge=0, le=1)

    # -- Presentation references expire, so "the best one" cannot resolve to a price
    # the customer saw an hour ago (ADR 0020).
    presentation_ttl_minutes: int = Field(default=30, ge=1)

    # -- Systems this deployment has. Policies have no table in the commerce core yet,
    # so the tool stays registered and answers `unavailable`: a system that exists and
    # is not wired is not a system the store lacks.
    enable_cart: bool = True
    enable_orders: bool = True
    enable_policies: bool = True
    enable_fulfillment: bool = True

    # -- Grounding gates (ADR 0021 and the catalogue rule). Each forces one read on a
    # turn's first round when the message matches.
    checkout_precedence: bool = True
    catalog_grounding_gate: bool = True
    variant_id_patterns: tuple[str, ...] = (r"\bsd_var_[a-z0-9]+\b", r"\bvar_[a-z0-9]{6,}\b")
    policy_grounding_gate: bool = True
    policy_intent_terms: tuple[str, ...] = (
        "return", "returns", "refund", "refunds", "exchange", "warranty", "guarantee",
        "cancel", "cancellation", "restocking", "fee", "fees", "shipping cost",
        "delivery cost", "gst", "invoice", "policy", "policies", "terms",
    )
    policy_intent_cues: tuple[str, ...] = (
        "?", "how", "what", "when", "can i", "could i", "do you", "does", "is there",
        "tell me", "explain", "how long", "how much",
    )
    order_grounding_gate: bool = True
    order_intent_terms: tuple[str, ...] = (
        "order", "orders", "delivery", "package", "parcel", "shipment", "tracking",
    )
    order_intent_cues: tuple[str, ...] = (
        "?", "where", "when", "status", "cancel", "change", "return", "refund", "late",
        "arrive", "arrived", "track", "missing", "damaged", "delayed",
    )

    def absent_tools(self) -> frozenset[str]:
        names: set[str] = set(FORBIDDEN_TOOLS)
        if not self.enable_cart:
            names |= {
                "get_cart", "add_to_cart", "update_cart_item", "remove_from_cart",
                "stage_checkout", "present_cart", "present_checkout",
            }
        if not self.enable_orders:
            names |= {"get_orders", "get_order_status", "present_order_status"}
        if not self.enable_policies:
            names.add("search_policies")
        if not self.enable_fulfillment:
            names.add("get_fulfillment_options")
        return frozenset(names)


class MerchantAgentConfig(CartisanAgentConfig):
    """The merchant surface. Its writes stage proposals and nothing else (ADR 0016);
    the tools that would apply one are in `FORBIDDEN_TOOLS` for both roles."""

    assistant_name: str = "the Cartisan merchant assistant"
    enable_cart: bool = False
    enable_fulfillment: bool = False
