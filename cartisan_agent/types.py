"""The runtime's domain models: what a commerce port returns, and the per-session
records the gates read.

Money is always integer minor units (paise), the same unit the commerce core stores,
so nothing in this runtime rounds a rupee. A payload that is shown to a person also
carries a formatted `price` label; the model quotes the label and never does the
arithmetic itself.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from commerce_common.types import ClockContext, remember

Surface = Literal["shopping", "merchant"]


def inr(minor: int) -> str:
    """Paise as the rupee label every payload shows. Whole rupees: the catalogue
    prices in whole rupees and a trailing `.00` reads as false precision."""
    return f"₹{minor // 100:,}"


class Variant(BaseModel):
    """One purchasable catalogue variant. Cartisan sells variants, not families: a
    `catalog_products` row groups them and is never itself added to a cart."""

    variant_id: str
    product_id: str
    sku: str
    title: str
    brand: str
    category: str | None = None
    price_minor: int
    currency: str = "INR"
    in_stock: bool = True
    sellable: int = 0
    options: dict[str, str] = Field(default_factory=dict)
    origin: str = "seeded"

    @property
    def price(self) -> str:
        return inr(self.price_minor)


class VariantDetails(Variant):
    description: str = ""
    specs: dict[str, str] = Field(default_factory=dict)
    capabilities: dict[str, str] = Field(default_factory=dict)
    requirements: list[str] = Field(default_factory=list)
    siblings: list[Variant] = Field(default_factory=list)


class SearchFilters(BaseModel):
    category: str | None = None
    brand: str | None = None
    min_price_minor: int | None = Field(default=None, ge=0)
    max_price_minor: int | None = Field(default=None, ge=0)
    in_stock_only: bool = True
    sort: Literal["relevance", "price_asc", "price_desc"] = "relevance"


class CompatibilityFinding(BaseModel):
    """One structured requirement evaluated against one candidate (ADR 0006). The
    explanation is the requirement row's own text; nothing here is model prose."""

    capability: str
    operator: str
    expected: str
    observed: str | None = None
    severity: Literal["blocking", "advisory"] = "blocking"
    satisfied: bool
    explanation: str


class CompatibilityVerdict(BaseModel):
    base_variant_id: str
    candidate_variant_id: str
    compatible: bool
    findings: list[CompatibilityFinding] = Field(default_factory=list)


class CartLine(BaseModel):
    variant_id: str
    title: str
    quantity: int = Field(ge=1)
    unit_price_minor: int
    amount_minor: int


class Cart(BaseModel):
    cart_id: str
    state_version: int
    currency: str = "INR"
    lines: list[CartLine] = Field(default_factory=list)
    subtotal_minor: int = 0

    @property
    def item_count(self) -> int:
        return sum(line.quantity for line in self.lines)


class StagedCheckout(BaseModel):
    """The immutable expiring preview `stage_checkout` creates. It moves no money and
    reserves no stock (ADR 0012); confirmation is the host's, never the model's."""

    stage_id: str
    cart_id: str
    cart_state_version: int
    state: str
    currency: str = "INR"
    lines: list[CartLine] = Field(default_factory=list)
    subtotal_minor: int
    shipping_minor: int = 0
    tax_minor: int = 0
    discount_minor: int = 0
    total_minor: int
    fulfillment_option: str
    constraints_note: str | None = None
    expires_at: str


class OrderLine(BaseModel):
    variant_id: str
    title: str
    quantity: int
    unit_price_minor: int


class Order(BaseModel):
    order_id: str
    status: str
    placed_at: datetime | str
    currency: str = "INR"
    total_minor: int
    lines: list[OrderLine] = Field(default_factory=list)
    payment_state: str | None = None


class Policy(BaseModel):
    policy_id: str
    title: str
    category: str | None = None
    content: str


class FulfillmentOption(BaseModel):
    method: Literal["delivery", "pickup", "shipping"]
    eta: str
    fee_minor: int = 0
    location: str | None = None


class Preferences(BaseModel):
    customer_id: str
    display_name: str | None = None
    email: str | None = None
    default_location: str | None = None
    preferences: dict[str, str] = Field(default_factory=dict)


class PageContext(BaseModel):
    page_type: Literal["home", "search", "product", "cart", "orders", "other"] = "home"
    variant_id: str | None = None
    query: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class SessionContext(ClockContext):
    """Everything the runtime knows about one turn's principal. `customer_id` comes
    from the verified Supabase principal and is never a request field (ADR 0010)."""

    conversation_id: str
    customer_id: str
    surface: Surface = "shopping"
    demo_run_id: str | None = None
    page: PageContext = Field(default_factory=PageContext)

    @property
    def session_id(self) -> str:
        # `commerce_common` helpers key locks and memory by `session_id`; for Cartisan
        # the conversation is that unit, and turns within it are serialized (ADR 0029).
        return self.conversation_id

    @property
    def user_id(self) -> str:
        return self.customer_id


class SessionState(BaseModel):
    """Per-conversation provenance the gates read. `seen_variants` records what the
    catalogue tools returned this session; `issued_items` records the server-issued
    presentation item references a cart write may name (ADR 0020)."""

    seen_variants: dict[str, Variant] = Field(default_factory=dict)
    issued_items: dict[str, str] = Field(default_factory=dict)  # item_ref -> variant_id
    checkout_turn: bool = False

    def remember_variants(self, variants: list[Variant]) -> None:
        for variant in variants:
            remember(self.seen_variants, variant.variant_id, variant)
