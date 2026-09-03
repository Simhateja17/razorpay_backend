from __future__ import annotations

import hashlib, hmac, json, os
from pathlib import Path
from typing import AsyncIterator

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from marketplace_backend.agent_service import AgentNarrator
from marketplace_backend.audit import AuditTrail
from marketplace_backend.merchant_backend import MerchantBackend
from marketplace_backend.store import Store
from marketplace_backend.storefront_backend import StorefrontBackend

db = Store(os.getenv("CARTISAN_DB_PATH"))
audit = AuditTrail(db)
shop = StorefrontBackend(db, audit)
merchant = MerchantBackend(db, audit, shop)
narrator = AgentNarrator()

app = FastAPI(title="Cartisan API", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=os.getenv("CORS_ORIGINS","http://localhost:3000").split(","),
                   allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

class ChatRequest(BaseModel):
    session_id: str = Field(min_length=1,max_length=100)
    message: str = Field(min_length=1,max_length=2000)

class CartRequest(BaseModel):
    session_id: str
    product_id: str
    quantity: int = Field(default=1,ge=0,le=10)
    reasoning: str = "Customer requested this cart change"

class CheckoutRequest(BaseModel):
    session_id: str
    reasoning: str = "Customer explicitly requested checkout"

class ProposalRequest(BaseModel):
    session_id: str
    kind: str
    target_id: str | None = None
    before: dict = Field(default_factory=dict)
    after: dict
    reasoning: str = Field(min_length=3)

class DecisionRequest(BaseModel):
    session_id: str
    decision: str

def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data,ensure_ascii=False)}\n\n"

async def one_event(event: str, data: dict) -> AsyncIterator[str]:
    yield sse(event, data)
    yield sse("done", {"ok":True})

@app.get("/health")
def health(): return {"status":"ok"}

@app.post("/chat/storefront")
async def storefront_chat(body: ChatRequest):
    products = shop.search(body.session_id, body.message, reasoning=f'Customer asked: "{body.message}"')
    cart = shop.cart_read(body.session_id)
    upsell = None
    excluded_ids = {p["id"] for p in products} | {x["product_id"] for x in cart["lines"]}
    for line in cart["lines"]:
        candidate = shop.cross_sell(
            body.session_id, line["product_id"],
            reasoning=f'One bounded catalog pairing for cart item {line["product_id"]}',
            excluded_ids=excluded_ids,
        )
        if candidate:
            upsell = {**candidate, "is_upsell": True}
            products.append(upsell)
            break
    fallback = f"I found {len(products)} in-stock catalog matches."
    text = await narrator.say("You are Cartisan's concise shopping assistant. Never invent products or prices.",
      f"Customer: {body.message}\nVerified matches: {json.dumps([{'name':p['name'],'price':p['price'],'is_upsell':p.get('is_upsell',False)} for p in products])}\n"
      "If one item is marked is_upsell, identify it as one optional pairing. Reply in at most 2 sentences.", fallback)
    payload={"id":f"m_{hashlib.sha1((body.session_id+body.message).encode()).hexdigest()[:10]}","role":"agent",
             "text":text,"why":"Only verified, in-stock catalog records were returned. Any is_upsell item is one code-bounded cross-sell for an item already in the cart.","products":products}
    return StreamingResponse(one_event("message",payload),media_type="text/event-stream")

@app.post("/chat/portal")
async def portal_chat(body: ChatRequest):
    snapshot=merchant.business_snapshot(body.session_id)
    catalog_context = [{"id":p["id"],"name":p["name"],"category":p["category"],"price":p["price"],"stock":p["stock"]}
                       for p in shop.products.values() if not p.get("options")]
    turn=await narrator.merchant_turn(
      f"Merchant request: {body.message}\nVerified snapshot: {json.dumps(snapshot)}\nVerified catalog: {json.dumps(catalog_context)}",
      "Sales are steady. I did not queue a change because I could not safely form a proposal.")
    proposal = turn.get("proposal")
    approval = None
    if proposal:
        try:
            kind,target_id,before,after,reasoning=merchant.validate_chat_proposal(proposal)
            approval=merchant.propose(body.session_id,kind,target_id,before,after,reasoning)
        except (TypeError,ValueError) as exc:
            audit.append(session_id=body.session_id,agent="merchant",action="proposal_rejected_by_guardrail",
                         reasoning="Model proposal failed a code-enforced bound",outcome="failed",gated=True,
                         result={"error":str(exc)})
    text=turn.get("reply") or "I reviewed the current snapshot."
    payload={"id":f"m_{hashlib.sha1((body.session_id+body.message).encode()).hexdigest()[:10]}","role":"agent",
             "text":text,"why":"Based on verified business and catalog data. Any proposed write is pending human approval; none was applied.",
             "approval":approval}
    return StreamingResponse(one_event("message",payload),media_type="text/event-stream")

@app.get("/catalog")
def catalog(): return [shop._public(p) for p in shop.products.values() if not p.get("variant_of")]
@app.get("/cart/{session_id}")
def cart(session_id: str): return shop.cart_read(session_id)
@app.post("/cart/items")
def add_cart(body: CartRequest): return shop.add_to_cart(body.session_id,body.product_id,body.quantity,body.reasoning)
@app.patch("/cart/items")
def update_cart(body: CartRequest): return shop.update_quantity(body.session_id,body.product_id,body.quantity,body.reasoning)
@app.delete("/cart/{session_id}/items/{product_id}")
def remove_cart(session_id: str,product_id: str): return shop.remove_from_cart(session_id,product_id)
@app.post("/checkout")
async def checkout(body: CheckoutRequest): return await shop.checkout_handoff(body.session_id,body.reasoning)
@app.get("/orders/{session_id}/{order_id}")
def order_status(session_id: str,order_id: str):
    return shop.order_status(session_id,order_id) or (_ for _ in ()).throw(HTTPException(404,"Order not found"))

@app.get("/portal/snapshot")
def snapshot(session_id: str): return merchant.business_snapshot(session_id)
@app.get("/portal/approvals")
def approvals(): return merchant.pending()
@app.post("/portal/approvals")
def propose(body: ProposalRequest): return merchant.propose(body.session_id,body.kind,body.target_id,body.before,body.after,body.reasoning)
@app.post("/portal/approvals/{change_id}/decision")
def decide(change_id: str,body: DecisionRequest): return merchant.decide(body.session_id,change_id,body.decision)

@app.get("/audit")
def audit_list(agent: str | None=None,limit: int=200): return audit.list(agent=agent,limit=limit)

@app.post("/webhook/razorpay")
async def razorpay_webhook(request: Request,x_razorpay_signature: str=Header(default="")):
    raw=await request.body(); secret=os.getenv("RAZORPAY_WEBHOOK_SECRET","")
    expected=hmac.new(secret.encode(),raw,hashlib.sha256).hexdigest()
    if not secret or not hmac.compare_digest(expected,x_razorpay_signature): raise HTTPException(401,"Invalid signature")
    event=json.loads(raw); entity=event.get("payload",{}).get("payment_link",{}).get("entity",{})
    link_id=entity.get("id"); status=entity.get("status") or event.get("event","").split(".")[-1]
    if link_id: db.execute("UPDATE orders SET status=? WHERE payment_link_id=?",(status,link_id))
    audit.append(session_id="webhook",agent="shopping",action="payment_status",reasoning="Verified Razorpay webhook",
                 outcome="failed" if status in {"failed","cancelled","expired"} else "ok",gated=True,
                 result={"payment_link_id":link_id,"status":status})
    return {"ok":True}
