from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from .audit import AuditTrail
from .mcp_client import RazorpayMCPClient
from .store import Store

STOPWORDS = {"the","and","for","with","want","need","show","some","under","this","that"}
MAX_QUANTITY = 10
MAX_CHECKOUT_RUPEES = 10_000
UPSELL_RATIO = .35


class StorefrontBackend:
    def __init__(self, store: Store, audit: AuditTrail, mcp: RazorpayMCPClient | None = None) -> None:
        self.store, self.audit, self.mcp = store, audit, mcp
        data = json.loads(Path(__file__).with_name("catalog.json").read_text())
        self.products = {p["id"]: p for p in data["products"]}

    def _public(self, p: dict) -> dict:
        variants = [self._public(v) for v in self.products.values() if v.get("variant_of") == p["id"]]
        stock = sum(v["stock"] for v in variants) if variants else p["stock"]
        prices = [v["price"] for v in variants if v["stock"] > 0]
        price = min(prices) if prices else p["price"]
        return {**p, "stock": stock, "in_stock": stock > 0, "price": price,
                "price_label": f"₹{price:,.0f}", "meta": f'{p["category"]} · {p["description"]}',
                "variants": variants or None}

    def search(self, session_id: str, query: str, filters: dict | None = None, limit: int = 4,
               reasoning: str = "Customer searched the catalog") -> list[dict]:
        terms = [t for t in query.lower().replace("₹", " ").split() if len(t) >= 3 and t not in STOPWORDS]
        candidates = [p for p in self.products.values() if not p.get("variant_of")]
        if filters and filters.get("category"):
            candidates = [p for p in candidates if p["category"].lower() == filters["category"].lower()]
        ranked = sorted(((sum(t in f'{p["name"]} {p["description"]} {p["category"]}'.lower() for t in terms), p)
                         for p in candidates), key=lambda x: x[0], reverse=True)
        found = [self._public(p) for score, p in ranked if score > 0][:limit]
        self.audit.append(session_id=session_id, agent="shopping", action="search", reasoning=reasoning,
                          result={"query":query,"product_ids":[p["id"] for p in found]})
        return found

    def details(self, session_id: str, product_id: str, reasoning: str = "Customer requested details") -> dict | None:
        value = self._public(self.products[product_id]) if product_id in self.products else None
        self.audit.append(session_id=session_id, agent="shopping", action="details", reasoning=reasoning,
                          outcome="ok" if value else "failed", result={"product_id":product_id})
        return value

    def cart_read(self, session_id: str) -> dict:
        rows = self.store.rows("SELECT product_id,quantity FROM carts WHERE session_id=?", (session_id,))
        lines, total = [], 0
        for row in rows:
            p = self.products.get(row["product_id"])
            if not p: continue
            amount = p["price"] * row["quantity"]; total += amount
            lines.append({"product_id":p["id"],"name":p["name"],"price":p["price"],
                          "quantity":row["quantity"],"amount":amount})
        return {"lines":lines,"total":total,"currency":"INR"}

    def add_to_cart(self, session_id: str, product_id: str, quantity: int = 1, reasoning: str = "Customer requested item") -> dict:
        p = self.products.get(product_id)
        if not p or p.get("options"): raise ValueError("Choose a purchasable product variant")
        quantity = max(1, min(quantity, MAX_QUANTITY))
        if p["stock"] < quantity: raise ValueError("Requested quantity is unavailable")
        self.store.execute("INSERT INTO carts VALUES (?,?,?) ON CONFLICT(session_id,product_id) DO UPDATE SET quantity=MIN(?,quantity+?)",
                           (session_id, product_id, quantity, MAX_QUANTITY, quantity))
        cart = self.cart_read(session_id)
        self.audit.append(session_id=session_id, agent="shopping", action="add_to_cart", reasoning=reasoning,
                          result={"product_id":product_id,"quantity":quantity})
        return cart

    def update_quantity(self, session_id: str, product_id: str, quantity: int, reasoning: str = "Customer changed quantity") -> dict:
        if quantity <= 0: return self.remove_from_cart(session_id, product_id, reasoning)
        self.store.execute("UPDATE carts SET quantity=? WHERE session_id=? AND product_id=?",
                           (min(quantity, MAX_QUANTITY), session_id, product_id))
        self.audit.append(session_id=session_id, agent="shopping", action="update_quantity", reasoning=reasoning,
                          result={"product_id":product_id,"quantity":quantity})
        return self.cart_read(session_id)

    def remove_from_cart(self, session_id: str, product_id: str, reasoning: str = "Customer removed item") -> dict:
        self.store.execute("DELETE FROM carts WHERE session_id=? AND product_id=?", (session_id, product_id))
        self.audit.append(session_id=session_id, agent="shopping", action="remove_from_cart", reasoning=reasoning,
                          result={"product_id":product_id})
        return self.cart_read(session_id)

    def cross_sell(self, session_id: str, source_id: str, reasoning: str,
                   excluded_ids: set[str] | None = None) -> dict | None:
        cart = self.cart_read(session_id)
        p = next((p for p in self.products.values() if p.get("cross_sell_of") == source_id), None)
        if not p or p["id"] in (excluded_ids or set()) or p["price"] > max(1, cart["total"]) * UPSELL_RATIO: return None
        self.audit.append(session_id=session_id, agent="shopping", action="propose_upsell", reasoning=reasoning,
                          gated=True, result={"product_id":p["id"],"source_id":source_id})
        return self._public(p)

    async def checkout_handoff(self, session_id: str, reasoning: str) -> dict:
        cart = self.cart_read(session_id)
        if not cart["lines"]: raise ValueError("Cart is empty")
        if cart["total"] > MAX_CHECKOUT_RUPEES: raise ValueError("Cart exceeds ₹10,000 checkout bound")
        if not self.mcp: self.mcp = RazorpayMCPClient()
        order_id = f"CTN-{uuid4().hex[:8].upper()}"
        link = await self.mcp.create_payment_link(amount=cart["total"] * 100, reference_id=order_id,
                                                  description=f"Cartisan order {order_id}")
        from datetime import UTC, datetime
        self.store.execute("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?)", (order_id, session_id, "created", cart["total"],
          link["id"], link["short_url"], self.store.dump(cart), datetime.now(UTC).isoformat()))
        self.audit.append(session_id=session_id, agent="shopping", action="checkout_handoff", reasoning=reasoning,
                          gated=True, result={"order_id":order_id,"payment_link_id":link["id"],"amount":cart["total"]})
        return {"order_id":order_id,"payment_link_id":link["id"],"pay_url":link["short_url"],**cart}

    def order_status(self, session_id: str, order_id: str) -> dict | None:
        rows = self.store.rows("SELECT * FROM orders WHERE id=? AND session_id=?", (order_id,session_id))
        return rows[0] if rows else None
