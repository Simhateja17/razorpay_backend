from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

from .audit import AuditTrail
from .store import Store
from .storefront_backend import StorefrontBackend


class MerchantBackend:
    """Read operations plus a strict propose -> approve/reject -> apply write boundary."""
    def __init__(self, store: Store, audit: AuditTrail, storefront: StorefrontBackend) -> None:
        self.store, self.audit, self.storefront = store, audit, storefront

    def business_snapshot(self, session_id: str) -> dict:
        rows = self.store.rows("SELECT amount FROM orders WHERE status='paid'")
        sales = sum(int(row["amount"]) for row in rows)
        order_count = len(rows)
        value = {"period":"all_time","currency":"INR","sales":sales,"orders":order_count,
                 "traffic":None,"conversion_rate":None,"average_order_value":round(sales/order_count,2) if order_count else 0,
                 "limitations":[{"source":"traffic_analytics","note":"Traffic and conversion are not connected."}]}
        self.audit.append(session_id=session_id,agent="merchant",action="business_snapshot",
                          reasoning="Merchant requested a performance overview",result=value)
        return value

    def metric_series(self, session_id: str, metric: str) -> dict:
        value = {"metric":metric,"period":"last_7_days","points":[],
                 "note":"Daily analytics are not connected."}
        self.audit.append(session_id=session_id,agent="merchant",action="metric_series",
                          reasoning=f"Merchant requested {metric} trend",result=value)
        return value

    def pricing_context(self, session_id: str, product_id: str) -> dict | None:
        p = self.storefront.products.get(product_id)
        value = None if not p else {"product_id":product_id,"price":p["price"],"currency":"INR",
          "floor":None,"min_price_basis":"Cost and margin data are not connected."}
        self.audit.append(session_id=session_id,agent="merchant",action="pricing_context",
                          reasoning="Merchant requested pricing context",result=value)
        return value

    def campaign_performance(self, session_id: str) -> list[dict]:
        value = []
        self.audit.append(session_id=session_id,agent="merchant",action="campaign_performance",
                          reasoning="Merchant requested campaign performance",result=value)
        return value

    def inventory_alerts(self, session_id: str) -> list[dict]:
        value = [{"product_id":p["id"],"name":p["name"],"stock":p["stock"],"kind":"low_stock"}
                 for p in self.storefront.products.values() if not p.get("options") and p["stock"] <= 12]
        self.audit.append(session_id=session_id,agent="merchant",action="inventory_alerts",
                          reasoning="Merchant requested inventory risks",result=value)
        return value

    def order_issues(self, session_id: str) -> list[dict]:
        value = [{"order_id":o["id"],"kind":"payment_failed","status":o["status"]}
                 for o in self.store.rows("SELECT id,status FROM orders WHERE status='failed'")]
        self.audit.append(session_id=session_id,agent="merchant",action="order_issues",
                          reasoning="Merchant requested order exceptions",result=value)
        return value

    def propose(self, session_id: str, kind: str, target_id: str | None, before: dict, after: dict,
                reasoning: str) -> dict:
        allowed = {"price_update","restock","pause_product","activate_product","promotion","content_edit","refund"}
        if kind not in allowed: raise ValueError("Unsupported merchant change")
        change_id, now = f"appr_{uuid4().hex[:12]}", datetime.now(UTC).isoformat()
        self.store.execute("INSERT INTO approvals VALUES (?,?,?,?,?,?,?,?,?)", (change_id,kind,target_id,
          self.store.dump(before),self.store.dump(after),reasoning,"pending",now,None))
        value = self.get_approval(change_id)
        self.audit.append(session_id=session_id,agent="merchant",action=f"propose_{kind}",reasoning=reasoning,
                          outcome="pending_approval",gated=True,result={"approval_id":change_id})
        return value

    def validate_chat_proposal(self, proposal: dict) -> tuple[str, str | None, dict, dict, str]:
        """Turn untrusted model output into one bounded, reviewable proposal."""
        kind, target_id = proposal.get("kind"), proposal.get("target_id")
        after, reasoning = proposal.get("after") or {}, str(proposal.get("reasoning") or "").strip()
        if not reasoning: raise ValueError("Proposal reasoning is required")
        p = self.storefront.products.get(target_id or "")
        if kind in {"price_update", "restock", "pause_product", "activate_product", "content_edit"} and not p:
            raise ValueError("Proposal target is not in the catalog")
        if kind == "price_update":
            if p.get("options"): raise ValueError("Price changes must target a purchasable SKU or variant")
            price = int(after.get("price", 0)); old = int(p["price"]); floor = round(old * .72)
            if price < floor or price < round(old * .8) or price > round(old * 1.2):
                raise ValueError("Price change exceeds the 20% or margin bound")
            return kind, target_id, {"price": old}, {"price": price}, reasoning
        if kind == "restock":
            quantity = int(after.get("quantity", 0))
            if not 1 <= quantity <= 500: raise ValueError("Restock must be between 1 and 500 units")
            return kind, target_id, {"stock": p["stock"]}, {"quantity": quantity}, reasoning
        if kind in {"pause_product", "activate_product"}:
            return kind, target_id, {"stock": p["stock"]}, {}, reasoning
        if kind == "content_edit":
            description = str(after.get("description", "")).strip()
            if not 5 <= len(description) <= 240: raise ValueError("Description must be 5-240 characters")
            return kind, target_id, {"description": p["description"]}, {"description": description}, reasoning
        if kind == "promotion":
            discount = int(after.get("discount_percent", 0)); cap = int(after.get("exposure_cap", 0))
            if not 1 <= discount <= 15 or not 1 <= cap <= 500_000:
                raise ValueError("Promotion exceeds discount or exposure bounds")
            return kind, target_id, {}, {"discount_percent": discount, "exposure_cap": cap}, reasoning
        raise ValueError("Unsupported merchant change")

    def get_approval(self, change_id: str) -> dict | None:
        rows = self.store.rows("SELECT * FROM approvals WHERE id=?", (change_id,))
        if not rows: return None
        row=rows[0]; row["before"]=json.loads(row.pop("before_json")); row["after"]=json.loads(row.pop("after_json"))
        return row

    def pending(self) -> list[dict]:
        return [self.get_approval(r["id"]) for r in self.store.rows("SELECT id FROM approvals WHERE status='pending' ORDER BY created_at")]

    def decide(self, session_id: str, change_id: str, decision: str) -> dict:
        if decision not in {"approved","rejected"}: raise ValueError("Decision must be approved or rejected")
        change = self.get_approval(change_id)
        if not change or change["status"] != "pending": raise ValueError("Change is not pending")
        if decision == "approved": self._apply(change)
        now = datetime.now(UTC).isoformat()
        self.store.execute("UPDATE approvals SET status=?,decided_at=? WHERE id=?", (decision,now,change_id))
        self.audit.append(session_id=session_id,agent="merchant",action=decision,reasoning=f'Human {decision} "{change["kind"]}"',
                          outcome=decision,gated=True,result={"approval_id":change_id})
        return self.get_approval(change_id)

    def _apply(self, change: dict) -> None:
        p = self.storefront.products.get(change["target_id"] or "")
        if change["kind"] == "price_update" and p:
            p["price"] = int(change["after"]["price"])
            self.store.execute("UPDATE products SET price=? WHERE id=?", (p["price"], p["id"]))
        elif change["kind"] == "restock" and p:
            p["stock"] += int(change["after"]["quantity"])
            self.store.execute("UPDATE products SET stock=? WHERE id=?", (p["stock"], p["id"]))
        elif change["kind"] == "pause_product" and p:
            p["active"] = False
            self.store.execute("UPDATE products SET active=0 WHERE id=?", (p["id"],))
        elif change["kind"] == "activate_product" and p:
            p["active"] = True
            self.store.execute("UPDATE products SET active=1 WHERE id=?", (p["id"],))
        elif change["kind"] == "content_edit" and p:
            p.update(change["after"])
            self.store.execute("UPDATE products SET description=? WHERE id=?", (p["description"], p["id"]))
        # Promotion/refund executors are not connected, so they remain approval-only.
