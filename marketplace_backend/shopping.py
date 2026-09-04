"""The host's shopping and checkout surface.

Everything the browser and the agent both need, sitting on one authority. The cart
here *is* the agent's cart: both go through `CoreCommercePort`, so the panel on the
right of the screen and `get_cart` in a tool call read the same variant-keyed row
and the same `state_version`. That is what retires the Phase 4 carry-over.

The checkout half is deliberately a sequence of small, separately-recorded steps,
because each one has a different authority behind it:

    stage      the agent may reach this, and it holds nothing
    confirm    the *customer* reaches this; it creates one order and holds stock
    dispatch   the *host* reaches this; it asks Razorpay for a link
    settle     only the *provider* reaches this, and only through a verified event

No method here is callable by a model. The agent's reach ends at `stage_checkout`
in `cartisan_agent.contracts`, and `FORBIDDEN_TOOLS` keeps it there (ADR 0005,
ADR 0015).
"""

from __future__ import annotations

from typing import Any

from cartisan_agent.core_port import CoreCommercePort
from cartisan_agent.outcomes import Conflict, Unavailable
from cartisan_agent.types import SessionContext

from .carts import ConflictError, IdempotencyLedger
from .checkout import CheckoutRepository, StageExpired, StageMismatch
from .evidence import Actor, Correlation, EvidenceLedger
from .inventory import InsufficientStock
from .payments import PaymentLinkDispatcher
from .recovery import order_recovery_actions
from .state_machines import TransitionError
from .store import Store


class CheckoutRefused(Exception):
    """A checkout the rules do not allow. Carries the sentence the customer sees."""


class ShoppingService:
    def __init__(
        self,
        store: Store,
        port: CoreCommercePort,
        checkout: CheckoutRepository,
        dispatcher: PaymentLinkDispatcher,
        ledger: EvidenceLedger | None = None,
    ) -> None:
        self.store, self.port, self.checkout = store, port, checkout
        self.dispatcher = dispatcher
        self.ledger = ledger or EvidenceLedger(store)
        self.idempotency = IdempotencyLedger(store)

    @staticmethod
    def session(customer_id: str, correlation: Correlation | None = None) -> SessionContext:
        """The browser's own session. It is a real principal doing a real thing, so it
        carries the request's lineage exactly as an agent turn does — that is what puts
        a click and the turn it interrupts on the same journey (ADR 0032)."""
        correlation = correlation or Correlation()
        return SessionContext(
            conversation_id=f"rest:{customer_id}", customer_id=customer_id,
            correlation_id=correlation.correlation_id, demo_run_id=correlation.demo_run_id)

    # ---------------------------------------------------------------- catalogue

    def catalog(self, limit: int = 60) -> list[dict]:
        """Active products with their purchasable variants, priced and stock-checked.

        Grouped by product because that is how a person shops, but every buyable id
        in the response is a *variant* id — the same id the cart, the stage, the
        order line and `add_to_cart` all use. One id space, end to end.
        """
        products = self.store.rows(
            "SELECT p.id, p.title, p.brand, p.description, p.origin, c.name AS category "
            "FROM catalog_products p LEFT JOIN catalog_categories c ON c.id = p.category_id "
            "WHERE p.status = 'active' ORDER BY p.id LIMIT ?",
            (max(1, limit),),
        )
        out = []
        for product in products:
            variants = [
                self._variant_dto(row)
                for row in self.store.rows(
                    "SELECT id, sku, title, options FROM catalog_variants "
                    "WHERE product_id = ? AND status = 'active' ORDER BY id",
                    (product["id"],),
                )
            ]
            if not variants:
                continue
            in_stock = [v for v in variants if v["in_stock"]]
            out.append({
                "product_id": product["id"],
                "title": product["title"],
                "brand": product["brand"],
                "category": product.get("category"),
                "description": product["description"],
                "origin": product.get("origin") or "seeded",
                # The headline price is the cheapest thing you can actually buy, so a
                # sold-out cheap variant cannot advertise a price that is unavailable.
                "from_price_minor": min((v["price_minor"] for v in in_stock), default=
                                        min(v["price_minor"] for v in variants)),
                "in_stock": bool(in_stock),
                "variants": variants,
            })
        return out

    def _variant_dto(self, row: dict) -> dict:
        sellable = self.port.sellable(row["id"])
        return {
            "variant_id": row["id"],
            "sku": row["sku"],
            "title": row["title"],
            "options": self.store.load(row["options"]) if row.get("options") else {},
            "price_minor": self.port.current_price(row["id"]),
            "sellable": sellable,
            "in_stock": sellable > 0,
        }

    # --------------------------------------------------------------------- cart

    async def cart(self, customer_id: str) -> dict:
        return _cart_dto(await self.port.get_cart(self.session(customer_id)), customer_id)

    async def add(self, customer_id: str, variant_id: str, quantity: int, *,
                  expected_version: int | None = None,
                  idempotency_key: str | None = None,
                  correlation: Correlation | None = None) -> dict:
        return await self._mutate(
            "add_to_cart", customer_id, idempotency_key,
            {"variant_id": variant_id, "quantity": quantity}, correlation,
            lambda session: self.port.add_to_cart(
                session, variant_id, quantity,
                expected_state_version=expected_version, idempotency_key=None))

    async def update(self, customer_id: str, variant_id: str, quantity: int, *,
                     expected_version: int | None = None,
                     idempotency_key: str | None = None,
                     correlation: Correlation | None = None) -> dict:
        if quantity <= 0:
            return await self.remove(customer_id, variant_id,
                                     expected_version=expected_version,
                                     idempotency_key=idempotency_key,
                                     correlation=correlation)
        return await self._mutate(
            "update_cart_item", customer_id, idempotency_key,
            {"variant_id": variant_id, "quantity": quantity}, correlation,
            lambda session: self.port.update_cart_item(
                session, variant_id, quantity,
                expected_state_version=expected_version, idempotency_key=None))

    async def remove(self, customer_id: str, variant_id: str, *,
                     expected_version: int | None = None,
                     idempotency_key: str | None = None,
                     correlation: Correlation | None = None) -> dict:
        return await self._mutate(
            "remove_from_cart", customer_id, idempotency_key, {"variant_id": variant_id},
            correlation,
            lambda session: self.port.remove_from_cart(
                session, variant_id,
                expected_state_version=expected_version, idempotency_key=None))

    async def _mutate(self, operation: str, customer_id: str, idempotency_key: str | None,
                      request: dict, correlation: Correlation | None, effect: Any) -> dict:
        """Run one cart mutation exactly once, whatever the network does.

        The idempotency ledger is checked and written around the effect rather than
        inside a callback, because the effect is a coroutine and
        `IdempotencyLedger.run` takes a synchronous one.

        The evidence row is written here, at the host boundary, and deliberately not
        in the port: the agent's identical call is already recorded once by
        `TurnStore.record_tool`, and recording it in the port as well would put two
        rows in the journey for one thing that happened.
        """
        correlation = correlation or Correlation()
        session = self.session(customer_id, correlation)
        if idempotency_key:
            recorded = self.store.rows(
                "SELECT operation, request_fingerprint, response_json FROM idempotency_records "
                "WHERE key=? AND principal_id=?", (idempotency_key, customer_id))
            if recorded:
                row = recorded[0]
                if (row["operation"] != operation
                        or row["request_fingerprint"] != IdempotencyLedger.fingerprint(request)):
                    raise ConflictError(
                        "This idempotency key was already used for a different request")
                return self.store.load(row["response_json"])
        actor = Actor("customer", customer_id, "shopping")
        try:
            cart = _cart_dto(await effect(session), customer_id)
        except Conflict as exc:
            self.ledger.record(
                actor=actor, action=operation, reason="Cart changed after it was read",
                outcome="conflict", target_type="cart", target_id=None,
                policy_checks={"error": str(exc)}, correlation=correlation)
            raise ConflictError(str(exc)) from exc
        except Unavailable as exc:
            self.ledger.record(
                actor=actor, action=operation, reason="The cart write could not be applied",
                outcome="unavailable", target_type="cart", target_id=None,
                policy_checks={"error": str(exc)}, correlation=correlation)
            raise ValueError(str(exc)) from exc
        self.ledger.record(
            actor=actor, action=operation, reason="Customer changed their cart in the browser",
            outcome="applied", target_type="cart", target_id=cart["cart_id"],
            state_ref={**request, "state_version": cart["state_version"]},
            correlation=correlation)
        if idempotency_key:
            self.store.execute(
                "INSERT INTO idempotency_records (key,principal_id,operation,request_fingerprint,"
                "response_json) VALUES (?,?,?,?,?)",
                (idempotency_key, customer_id, operation,
                 IdempotencyLedger.fingerprint(request), self.store.dump(cart)))
        return cart

    # ----------------------------------------------------------------- checkout

    async def stage(self, customer_id: str, *, fulfillment_option: str = "standard",
                    note: str | None = None,
                    correlation: Correlation | None = None) -> dict:
        """Price the authoritative cart into an expiring preview. Holds nothing."""
        try:
            staged = await self.port.stage_checkout(
                self.session(customer_id, correlation),
                fulfillment_option=fulfillment_option, note=note)
        except Unavailable as exc:
            raise CheckoutRefused(str(exc)) from exc
        return staged.model_dump()

    async def confirm(self, customer_id: str, stage_id: str, *,
                      idempotency_key: str | None = None,
                      correlation: Correlation | None = None) -> dict:
        """The customer's confirmation: one order, stock held, one link requested.

        A replay returns the first result. Without that, a double-tap on a slow
        connection would confirm the same preview twice — and the second confirm
        would be refused only *after* the first had already created the order, so
        the customer would see a failure for a purchase that had actually worked.
        """
        correlation = correlation or Correlation()
        if idempotency_key:
            recorded = self.store.rows(
                "SELECT response_json FROM idempotency_records WHERE key=? AND principal_id=?",
                (idempotency_key, customer_id))
            if recorded:
                return self.store.load(recorded[0]["response_json"])

        cart = await self.port.get_cart(self.session(customer_id, correlation))
        try:
            order = self.checkout.confirm(
                stage_id=stage_id, customer_id=customer_id,
                current_cart_state_version=cart.state_version,
                origin="live_app", correlation=correlation)
        except PermissionError as exc:
            raise CheckoutRefused("That checkout belongs to another account.") from exc
        except StageExpired as exc:
            raise CheckoutRefused(
                "That checkout preview expired. Review your cart and try again.") from exc
        except StageMismatch as exc:
            raise CheckoutRefused(
                "Your cart changed after that preview was built. Review it and try again.") from exc
        except InsufficientStock as exc:
            raise CheckoutRefused(
                "One of those items sold out while you were checking out. "
                "Nothing was charged and nothing was reserved.") from exc
        except TransitionError as exc:
            raise CheckoutRefused("That checkout preview is no longer open.") from exc

        payment = await self.open_payment(customer_id, order["id"], correlation=correlation)
        # The cart is retired only once the order exists, so a refused confirmation
        # leaves the customer with the cart they still have.
        self.store.execute(
            "UPDATE customer_carts SET status='checked_out' WHERE id=? AND status='active'",
            (cart.cart_id,))
        response = {"order": self._titled(_order_dto(self.checkout.read_order(order["id"]))),
                    "payment": payment}
        if idempotency_key:
            self.store.execute(
                "INSERT INTO idempotency_records (key,principal_id,operation,request_fingerprint,"
                "response_json) VALUES (?,?,?,?,?)",
                (idempotency_key, customer_id, "confirm_checkout",
                 IdempotencyLedger.fingerprint({"stage_id": stage_id}),
                 self.store.dump(response)))
        return response

    async def open_payment(self, customer_id: str, order_id: str, *,
                           correlation: Correlation | None = None) -> dict:
        """Open (or reuse) an attempt and deliver its link request.

        This is also the retry path: a second call after a decline creates a second
        attempt on the *same* internal order, so a retry never becomes a second
        order and never re-reserves stock the customer already holds (ADR 0030).
        """
        if correlation is None:
            correlation = self._order_correlation(self.checkout.read_order(order_id))
        try:
            attempt = self.checkout.open_attempt(
                order_id=order_id, customer_id=customer_id, correlation=correlation)
        except PermissionError as exc:
            raise CheckoutRefused("That order belongs to another account.") from exc
        except TransitionError as exc:
            raise CheckoutRefused(str(exc)) from exc
        await self.dispatcher.drain()
        rows = self.store.rows("SELECT * FROM payment_attempts WHERE id=?", (attempt["id"],))
        current = rows[0] if rows else attempt
        return {
            "attempt_id": current["id"],
            "status": current["status"],
            "amount_minor": current["amount_minor"],
            "currency": current["currency"],
            "provider_reference": current.get("provider_reference"),
            "pay_url": current.get("provider_link_url"),
        }

    def redirect_returned(self, customer_id: str, order_id: str, *,
                          correlation: Correlation | None = None) -> dict:
        """The customer came back from the provider. That is not payment (ADR 0013)."""
        order = self._own_order(customer_id, order_id)
        if order["status"] == "pending_payment":
            order = self.checkout.mark_verification_pending(
                order_id, correlation=correlation or self._order_correlation(order))
        return self._titled(_order_dto(order))

    @staticmethod
    def _order_correlation(order: dict) -> Correlation:
        """The journey this order already belongs to.

        A redirect or a retry arrives on its own request, but it is a later step of
        the journey that created the order — so it continues that lineage rather than
        opening a second one for the same purchase.
        """
        return Correlation(correlation_id=order.get("correlation_id") or Correlation().correlation_id,
                           demo_run_id=order.get("demo_run_id"))

    # ------------------------------------------------------------------- orders

    def order(self, customer_id: str, order_id: str) -> dict:
        return self._titled(_order_dto(self._own_order(customer_id, order_id)))

    def orders(self, customer_id: str, limit: int = 20) -> list[dict]:
        return [self._titled(_order_dto(self.checkout.read_order(row["id"])))
                for row in self.checkout.orders_for(customer_id, limit)]

    def _titled(self, order: dict) -> dict:
        """Name the order's lines.

        The order line stores the variant id and the price it was bought at, and
        deliberately not the title: the price is the historical fact the order must
        preserve, while the name is a catalogue lookup. Reading it here keeps the
        order honest about what was charged and still shows a person a product name
        rather than `sd_prd_air_purifier_0_v0`.
        """
        for line in order["lines"]:
            named = self.store.rows(
                "SELECT title FROM catalog_variants WHERE id = ?", (line["variant_id"],))
            # A variant the catalogue has since dropped keeps its id, which is more
            # honest than inventing a name for something no longer listed.
            line["title"] = named[0]["title"] if named else line["variant_id"]
        return order

    def _own_order(self, customer_id: str, order_id: str) -> dict:
        try:
            order = self.checkout.read_order(order_id)
        except ValueError as exc:
            raise LookupError("Order not found") from exc
        if order["customer_id"] != customer_id:
            # Not "forbidden": a customer learning that someone else's order id is
            # real is itself a leak. An order they do not own does not exist.
            raise LookupError("Order not found")
        return order


def _cart_dto(cart: Any, customer_id: str) -> dict:
    return {
        "cart_id": cart.cart_id,
        "customer_id": customer_id,
        "state_version": cart.state_version,
        "currency": "INR",
        "subtotal_minor": cart.subtotal_minor,
        "lines": [
            {
                "variant_id": line.variant_id,
                "title": line.title,
                "quantity": line.quantity,
                "unit_price_minor": line.unit_price_minor,
                "amount_minor": line.amount_minor,
            }
            for line in cart.lines
        ],
    }


def _order_dto(order: dict) -> dict:
    """What the browser is allowed to know about an order.

    `paid` is reported only when the order says `paid` *and* the money is all there,
    so a partially-captured or optimistically-marked order can never render as paid.
    """
    return {
        "order_id": order["id"],
        "status": order["status"],
        "paid": order["status"] == "paid"
                and int(order["amount_paid_minor"]) >= int(order["total_minor"]),
        "currency": order["currency"],
        "subtotal_minor": order["subtotal_minor"],
        "shipping_minor": order["shipping_minor"],
        "tax_minor": order["tax_minor"],
        "discount_minor": order["discount_minor"],
        "total_minor": order["total_minor"],
        "amount_paid_minor": order["amount_paid_minor"],
        # The origin label travels with the order rather than being inferred from
        # where it is shown, so a reader never has to guess whether they are looking
        # at seeded history, a live app purchase, or a scenario pack (ADR 0008, 0032).
        "origin": order["origin"],
        # The lineage this purchase belongs to, so an order links straight to its
        # journey, and what can still be done about it if it is stuck (ADR 0030).
        "correlation_id": order.get("correlation_id"),
        "recovery_actions": order_recovery_actions(order["status"]),
        "created_at": str(order["created_at"]),
        "lines": [
            {
                "variant_id": line["variant_id"],
                "quantity": line["quantity"],
                "unit_price_minor": line["unit_price_minor"],
                "amount_minor": line["amount_minor"],
            }
            for line in order["lines"]
        ],
        "attempts": [
            {
                "attempt_id": attempt["id"],
                "status": attempt["status"],
                "amount_minor": attempt["amount_minor"],
                "provider_reference": attempt.get("provider_reference"),
                "failure_reason": attempt.get("failure_reason"),
                # Carried so a client that lost its in-memory checkout state (a
                # reload, a new tab) can rebuild the payment panel from `GET
                # /orders/{id}` alone, rather than needing a live handoff response.
                "pay_url": attempt.get("provider_link_url"),
            }
            for attempt in order["attempts"]
        ],
    }


__all__ = ["ShoppingService", "CheckoutRefused"]
