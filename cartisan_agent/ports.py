"""The one integration surface the runtime sits on.

Every method acts for the customer in `session`, whose identity came from a verified
Supabase principal (ADR 0010); no method takes a customer id from the model. Nothing
here places an order, moves money, creates a payment link, or reserves stock: the
farthest a shopping tool reaches is an expiring preview (ADR 0012, ADR 0015).

Keeping this a port rather than a set of direct repository calls is what lets the
transcript evaluations drive the real turn loop, the real gates, and the real
persistence against a scripted catalogue, and what will let Phase 5 replace the
adapter under it without touching a contract, a gate, or a prompt.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from .types import (
    Cart,
    CompatibilityVerdict,
    FulfillmentOption,
    Order,
    Policy,
    Preferences,
    SearchFilters,
    SessionContext,
    StagedCheckout,
    Variant,
    VariantDetails,
)


class CommercePort(ABC):
    # -- catalogue ------------------------------------------------------------

    @abstractmethod
    async def search_products(
        self,
        session: SessionContext,
        query: str,
        filters: SearchFilters | None = None,
        limit: int = 8,
    ) -> list[Variant]:
        """The closest matches, best first, at most `limit`; an empty list when nothing
        matches. Every result is purchasable as it stands."""

    @abstractmethod
    async def get_product_details(
        self, session: SessionContext, variant_id: str
    ) -> VariantDetails | None:
        """The full record for one variant, or None when the id is unknown."""

    @abstractmethod
    async def check_compatibility(
        self, session: SessionContext, base_variant_id: str, candidate_variant_id: str
    ) -> CompatibilityVerdict:
        """The structured requirement rules evaluated, one finding per rule (ADR 0006).
        Raises `Unavailable` when either id is unknown; never guesses."""

    # -- cart -----------------------------------------------------------------

    @abstractmethod
    async def get_cart(self, session: SessionContext) -> Cart:
        """The customer's one durable active cart (ADR 0022), with its state version."""

    @abstractmethod
    async def add_to_cart(
        self,
        session: SessionContext,
        variant_id: str,
        quantity: int,
        *,
        expected_state_version: int | None = None,
        idempotency_key: str | None = None,
    ) -> Cart:
        """Add units of a variant the gates have already cleared. A stale
        `expected_state_version` raises `Conflict` rather than overwriting (ADR 0029).
        Stock is not reserved here; reservation happens on confirmation (ADR 0012)."""

    @abstractmethod
    async def update_cart_item(
        self,
        session: SessionContext,
        variant_id: str,
        quantity: int,
        *,
        expected_state_version: int | None = None,
        idempotency_key: str | None = None,
    ) -> Cart:
        """Set a line to `quantity`."""

    @abstractmethod
    async def remove_from_cart(
        self,
        session: SessionContext,
        variant_id: str,
        *,
        expected_state_version: int | None = None,
        idempotency_key: str | None = None,
    ) -> Cart:
        """Remove a line."""

    @abstractmethod
    async def stage_checkout(
        self,
        session: SessionContext,
        *,
        fulfillment_option: str,
        note: str | None = None,
        idempotency_key: str | None = None,
    ) -> StagedCheckout:
        """Price the authoritative cart into an immutable expiring preview. Raises
        `Unavailable` for an empty cart or a line that can no longer be sold."""

    @abstractmethod
    async def read_stage(self, session: SessionContext, stage_id: str) -> StagedCheckout | None:
        """One of this customer's own staged previews, for the checkout card to render."""

    # -- customer context, orders, policies, fulfillment -----------------------

    @abstractmethod
    async def get_preferences(self, session: SessionContext) -> Preferences:
        """The customer's profile. Read before every turn; no tool writes to it."""

    @abstractmethod
    async def get_orders(self, session: SessionContext, limit: int = 5) -> list[Order]:
        """The customer's own orders, newest first."""

    @abstractmethod
    async def get_order(self, session: SessionContext, order_id: str) -> Order | None:
        """One of the customer's own orders, or None when the id is unknown or belongs
        to someone else."""

    @abstractmethod
    async def search_policies(self, session: SessionContext, query: str) -> list[Policy]:
        """Help and policy passages matching `query`."""

    @abstractmethod
    async def get_fulfillment_options(
        self, session: SessionContext, variant_ids: list[str]
    ) -> list[FulfillmentOption]:
        """Delivery and pickup options for up to twenty variants."""
