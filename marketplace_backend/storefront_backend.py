from __future__ import annotations

import json
import re
from uuid import uuid4

from .audit import AuditTrail
from .carts import CartRepository, ConflictError, IdempotencyLedger
from .mcp_client import RazorpayMCPClient
from .routing import classify, is_add_request, is_checkout_request, is_relative_add_request
from .store import Store

STOPWORDS = {
    "the", "and", "for", "with", "want", "need", "show", "some", "under", "this", "that",
    "please", "can", "you", "could", "would", "add", "put", "buy", "purchase", "get",
    "include", "order", "place", "to", "into", "in", "my", "cart", "basket", "bag", "me",
    "one", "of",
}
MAX_CHECKOUT_RUPEES = 10_000
UPSELL_RATIO = .35


class StorefrontBackend:
    def __init__(self, store: Store, audit: AuditTrail, mcp: RazorpayMCPClient | None = None) -> None:
        self.store, self.audit, self.mcp = store, audit, mcp
        self._last_searches: dict[str, list[dict]] = {}
        self.reload_products()
        # Cart ownership is derived from the verified principal, so the repository
        # is keyed by customer id and shared by every path — UI, REST, and agent.
        self.carts = CartRepository(store, lambda: self.products)
        self.idempotency = IdempotencyLedger(store)

    def reload_products(self) -> None:
        rows = self.store.rows("SELECT * FROM products WHERE active=1 ORDER BY id")
        products = []
        for row in rows:
            row["active"] = bool(row["active"])
            for source, target in (("options_json", "options"), ("option_values_json", "option_values")):
                value = row.pop(source)
                if value:
                    row[target] = json.loads(value)
            products.append(row)
        self.products = {product["id"]: product for product in products}

    def _public(self, p: dict) -> dict:
        variants = [self._public(v) for v in self.products.values() if v.get("variant_of") == p["id"]]
        stock = sum(v["stock"] for v in variants) if variants else p["stock"]
        prices = [v["price"] for v in variants if v["stock"] > 0]
        price = min(prices) if prices else p["price"]
        return {**p, "stock": stock, "in_stock": stock > 0, "price": price,
                "price_label": f"₹{price:,.0f}", "meta": f'{p["category"]} · {p["description"]}',
                "variants": variants or None}

    # Intent routing is deterministic and lives in routing.py, where checkout has
    # unconditional precedence over search and cart addition (ADR 0021).
    classify_intent = staticmethod(classify)
    is_add_request = staticmethod(is_add_request)
    is_checkout_request = staticmethod(is_checkout_request)
    is_relative_add_request = staticmethod(is_relative_add_request)

    @staticmethod
    def _search_terms(query: str) -> list[str]:
        # Keep numeric edition/size tokens (for example, ``Fast Charger 4``), while
        # ignoring punctuation and command filler around the product name.
        tokens = re.findall(r"[a-z]+|\d+", query.casefold())
        return [token for token in tokens if token not in STOPWORDS and (len(token) >= 3 or token.isdigit())]

    def search(self, session_id: str, query: str, filters: dict | None = None, limit: int = 4,
               reasoning: str = "Customer searched the catalog") -> list[dict]:
        terms = self._search_terms(query)
        candidates = [p for p in self.products.values() if not p.get("variant_of")]
        if filters and filters.get("category"):
            candidates = [p for p in candidates if p["category"].lower() == filters["category"].lower()]
        ranked = sorted(((sum(t in f'{p["name"]} {p["description"]} {p["category"]}'.lower() for t in terms), p)
                         for p in candidates), key=lambda x: x[0], reverse=True)
        found = []
        for score, product in ranked:
            if score <= 0:
                break
            public = self._public(product)
            if not public["in_stock"]:
                continue
            found.append(public)
            if len(found) >= limit:
                break
        # Keep the latest non-empty verified result set so a follow-up such as
        # "add one of them" can resolve against what this shopper just saw.
        if found:
            self._last_searches[session_id] = list(found)
        self.audit.append(session_id=session_id, agent="shopping", action="search", reasoning=reasoning,
                          result={"query":query,"product_ids":[p["id"] for p in found]})
        return found

    def last_search(self, session_id: str) -> list[dict]:
        return list(self._last_searches.get(session_id, []))

    @staticmethod
    def _purchasable_result(product: dict) -> dict | None:
        if product.get("options"):
            return next((variant for variant in product.get("variants") or [] if variant.get("in_stock")), None)
        return product if product.get("in_stock") else None

    def add_best_match(self, customer_id: str, products: list[dict], reasoning: str) -> tuple[dict | None, dict]:
        """Add the highest-ranked verified result, selecting an in-stock variant when needed."""
        for product in products:
            target = self._purchasable_result(product)
            if not target:
                continue
            try:
                cart = self.add_to_cart(customer_id, target["id"], 1, reasoning)
            except ValueError:
                continue
            return target, cart
        return None, self.cart_read(customer_id)

    def details(self, session_id: str, product_id: str, reasoning: str = "Customer requested details") -> dict | None:
        value = self._public(self.products[product_id]) if product_id in self.products else None
        self.audit.append(session_id=session_id, agent="shopping", action="details", reasoning=reasoning,
                          outcome="ok" if value else "failed", result={"product_id":product_id})
        return value

    # ------------------------------------------------------------------ cart
    # Every cart path below resolves the authenticated customer, so the cart the
    # browser renders and the cart the agent reads are the same durable row.

    def cart_read(self, customer_id: str) -> dict:
        return self.carts.read(customer_id)

    def add_to_cart(self, customer_id: str, product_id: str, quantity: int = 1,
                    reasoning: str = "Customer requested item", *, expected_version: int | None = None,
                    idempotency_key: str | None = None) -> dict:
        def effect() -> dict:
            cart = self.carts.add(customer_id, product_id, quantity, expected_version=expected_version)
            self.reload_products()
            self.audit.append(session_id=customer_id, agent="shopping", action="add_to_cart",
                              reasoning=reasoning,
                              result={"product_id":product_id,"quantity":quantity,
                                      "cart_id":cart["cart_id"],"state_version":cart["state_version"]})
            return cart

        return self.idempotency.run(
            principal_id=customer_id, operation="add_to_cart", key=idempotency_key,
            request={"product_id": product_id, "quantity": quantity}, effect=effect)

    def update_quantity(self, customer_id: str, product_id: str, quantity: int,
                        reasoning: str = "Customer changed quantity", *, expected_version: int | None = None,
                        idempotency_key: str | None = None) -> dict:
        def effect() -> dict:
            cart = self.carts.set_quantity(customer_id, product_id, quantity, expected_version=expected_version)
            self.reload_products()
            self.audit.append(session_id=customer_id, agent="shopping", action="update_quantity",
                              reasoning=reasoning,
                              result={"product_id":product_id,"quantity":quantity,
                                      "cart_id":cart["cart_id"],"state_version":cart["state_version"]})
            return cart

        return self.idempotency.run(
            principal_id=customer_id, operation="update_quantity", key=idempotency_key,
            request={"product_id": product_id, "quantity": quantity}, effect=effect)

    def remove_from_cart(self, customer_id: str, product_id: str,
                         reasoning: str = "Customer removed item", *, expected_version: int | None = None,
                         idempotency_key: str | None = None) -> dict:
        def effect() -> dict:
            cart = self.carts.remove(customer_id, product_id, expected_version=expected_version)
            self.reload_products()
            self.audit.append(session_id=customer_id, agent="shopping", action="remove_from_cart",
                              reasoning=reasoning,
                              result={"product_id":product_id,"cart_id":cart["cart_id"],
                                      "state_version":cart["state_version"]})
            return cart

        return self.idempotency.run(
            principal_id=customer_id, operation="remove_from_cart", key=idempotency_key,
            request={"product_id": product_id}, effect=effect)

    def cross_sell(self, customer_id: str, source_id: str, reasoning: str,
                   excluded_ids: set[str] | None = None) -> dict | None:
        cart = self.cart_read(customer_id)
        p = next((p for p in self.products.values() if p.get("cross_sell_of") == source_id), None)
        if (not p or p["id"] in (excluded_ids or set()) or p["stock"] <= 0
                or p["price"] > max(1, cart["total"]) * UPSELL_RATIO):
            return None
        self.audit.append(session_id=customer_id, agent="shopping", action="propose_upsell", reasoning=reasoning,
                          gated=True, result={"product_id":p["id"],"source_id":source_id})
        return self._public(p)

    # -------------------------------------------------------------- checkout

    def stage_checkout(self, customer_id: str, reasoning: str) -> dict:
        """Validate the authoritative cart without creating an order or payment link."""
        cart = self.cart_read(customer_id)
        error = self._checkout_block(cart)
        if error:
            self.audit.append(session_id=customer_id, agent="shopping", action="stage_checkout",
                              reasoning=reasoning, outcome="failed", gated=True,
                              result={"error":error,"amount":cart["total"]})
            raise ValueError(error)
        self.audit.append(session_id=customer_id, agent="shopping", action="stage_checkout", reasoning=reasoning,
                          gated=True, result={"cart_id":cart["cart_id"],"state_version":cart["state_version"],
                                              "product_ids":[line["product_id"] for line in cart["lines"]],
                                              "amount":cart["total"]})
        return cart

    @staticmethod
    def _checkout_block(cart: dict) -> str | None:
        if not cart["lines"]:
            return "Cart is empty"
        if cart["total"] > MAX_CHECKOUT_RUPEES:
            return "Cart exceeds \u20b910,000 checkout bound"
        return None

    async def checkout_handoff(self, customer_id: str, reasoning: str, *,
                               expected_version: int | None = None,
                               idempotency_key: str | None = None) -> dict:
        # A replay is answered from the ledger first: the original call retired the
        # cart it was staged from, so re-validating would wrongly report it empty.
        if idempotency_key:
            recorded = self.store.rows(
                "SELECT response_json FROM idempotency_records WHERE key=? AND principal_id=?",
                (idempotency_key, customer_id))
            if recorded:
                return self.store.load(recorded[0]["response_json"])

        cart = self.cart_read(customer_id)
        error = self._checkout_block(cart)
        if error:
            self.audit.append(session_id=customer_id, agent="shopping", action="checkout_handoff",
                              reasoning=reasoning, outcome="failed", gated=True,
                              result={"error":error,"amount":cart["total"]})
            raise ValueError(error)
        if expected_version is not None and cart["state_version"] != expected_version:
            raise ConflictError(
                f"Cart changed since you last read it (expected version {expected_version}, "
                f"found {cart['state_version']})")

        async def effect() -> dict:
            if not self.mcp:
                self.mcp = RazorpayMCPClient()
            order_id = f"CTN-{uuid4().hex[:8].upper()}"
            link = await self.mcp.create_payment_link(amount=cart["total"] * 100, reference_id=order_id,
                                                      description=f"Cartisan order {order_id}")
            from datetime import UTC, datetime
            self.store.execute(
                "INSERT INTO orders (id,session_id,customer_id,status,amount,payment_link_id,payment_url,"
                "payload,created_at,origin) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (order_id, customer_id, customer_id, "created", cart["total"], link["id"], link["short_url"],
                 self.store.dump(cart), datetime.now(UTC).isoformat(), "razorpay_test"))
            # The order holds a complete cart snapshot; retire the active cart so a
            # later turn cannot add to an already-staged checkout.
            self.carts.close(customer_id, "checked_out")
            self.audit.append(session_id=customer_id, agent="shopping", action="checkout_handoff",
                              reasoning=reasoning, gated=True,
                              result={"order_id":order_id,"payment_link_id":link["id"],
                                      "cart_id":cart["cart_id"],"amount":cart["total"]})
            return {"order_id":order_id,"payment_link_id":link["id"],"pay_url":link["short_url"],**cart}

        # Even without a caller-supplied key, one cart at one version can produce at
        # most one payment link, so a double-submit cannot charge the customer twice.
        key = idempotency_key or f"checkout:{cart['cart_id']}:{cart['state_version']}"
        fingerprint = {"cart_id": cart["cart_id"], "state_version": cart["state_version"]}
        recorded = self.store.rows(
            "SELECT response_json FROM idempotency_records WHERE key=? AND principal_id=?", (key, customer_id))
        if recorded:
            return self.store.load(recorded[0]["response_json"])
        response = await effect()
        self.store.execute(
            "INSERT INTO idempotency_records (key,principal_id,operation,request_fingerprint,response_json) "
            "VALUES (?,?,?,?,?)",
            (key, customer_id, "checkout_handoff", IdempotencyLedger.fingerprint(fingerprint),
             self.store.dump(response)))
        return response

    def order_status(self, customer_id: str, order_id: str) -> dict | None:
        """Orders are read by owner, so one customer can never read another's order."""
        rows = self.store.rows(
            "SELECT * FROM orders WHERE id=? AND customer_id=?", (order_id, customer_id))
        return rows[0] if rows else None
