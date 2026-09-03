"""The legacy flat catalogue, kept for the merchant surfaces alone.

Shopping left this file in Phase 5: the catalogue, the cart, checkout and orders all
read the normalized commerce core now, through `CoreCommercePort` and
`marketplace_backend.shopping`. What remains is the flat `products` table that
`MerchantBackend` still stages price and inventory changes against, and the search
it uses to describe them. Phase 6 migrates that, and this file goes with it.
"""

from __future__ import annotations

import json
import re

from .audit import AuditTrail
from .routing import classify, is_add_request, is_checkout_request, is_relative_add_request
from .store import Store

STOPWORDS = {
    "the", "and", "for", "with", "want", "need", "show", "some", "under", "this", "that",
    "please", "can", "you", "could", "would", "add", "put", "buy", "purchase", "get",
    "include", "order", "place", "to", "into", "in", "my", "cart", "basket", "bag", "me",
    "one", "of",
}


class StorefrontBackend:
    def __init__(self, store: Store, audit: AuditTrail) -> None:
        self.store, self.audit = store, audit
        self._last_searches: dict[str, list[dict]] = {}
        self.reload_products()

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

    def details(self, session_id: str, product_id: str, reasoning: str = "Customer requested details") -> dict | None:
        value = self._public(self.products[product_id]) if product_id in self.products else None
        self.audit.append(session_id=session_id, agent="shopping", action="details", reasoning=reasoning,
                          outcome="ok" if value else "failed", result={"product_id":product_id})
        return value
