from __future__ import annotations

import hashlib, hmac, json, os, re
from pathlib import Path
from typing import AsyncIterator

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from marketplace_backend.agent_service import AgentNarrator
from marketplace_backend.audit import AuditTrail
from marketplace_backend.carts import ConflictError
from marketplace_backend.routing import Intent
from marketplace_backend.identity import AuthenticationError, IdentityService, Principal
from marketplace_backend.merchant_backend import MerchantBackend
from marketplace_backend.store import Store
from marketplace_backend.storefront_backend import StorefrontBackend

db = Store(
    path=os.getenv("CARTISAN_DB_PATH"),
    database_url=os.getenv("SUPABASE_DATABASE_URL"),
)
audit = AuditTrail(db)
identity = IdentityService(db)
shop = StorefrontBackend(db, audit)
merchant = MerchantBackend(db, audit, shop)
narrator = AgentNarrator()

app = FastAPI(title="Cartisan API", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=os.getenv("CORS_ORIGINS","http://localhost:3000").split(","),
                   allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

# Identity is never a request field. `conversation_id` groups a chat thread; it
# carries no authority, and the cart it reads is the authenticated customer's.
class ChatRequest(BaseModel):
    conversation_id: str = Field(default="default",min_length=1,max_length=100)
    message: str = Field(min_length=1,max_length=2000)

class CartRequest(BaseModel):
    product_id: str
    quantity: int = Field(default=1,ge=0,le=10)
    reasoning: str = "Customer requested this cart change"
    expected_version: int | None = None
    idempotency_key: str | None = Field(default=None,max_length=200)

class CheckoutRequest(BaseModel):
    reasoning: str = "Customer explicitly requested checkout"
    expected_version: int | None = None
    idempotency_key: str | None = Field(default=None,max_length=200)

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


def require_customer(authorization: str | None = Header(default=None)) -> Principal:
    """Resolve the request's principal from a verified Supabase session."""
    try:
        principal = identity.principal(authorization)
    except AuthenticationError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    if principal.role != "customer":
        raise HTTPException(status_code=403, detail="This action requires a customer account")
    return principal

def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data,ensure_ascii=False)}\n\n"


def format_inr(value: float) -> str:
    return f"₹{value:,.0f}"


def normalize_inr(text: str) -> str:
    """Keep model narration aligned with the catalog's INR currency."""
    return re.sub(r"\$\s*(?=\d)", "₹", text)

async def one_event(event: str, data: dict) -> AsyncIterator[str]:
    yield sse(event, data)
    yield sse("done", {"ok":True})

async def streamed_message(payload: dict, text_stream: AsyncIterator[str] | None) -> AsyncIterator[str]:
    """Emit narration as `text_delta` chunks as Claude generates them, then the
    complete `message` (same shape whether or not a client reads the deltas),
    then `done`. `text_stream=None` skips straight to `message` for turns whose
    text is already fully known in code (checkout, add-to-cart, no-match)."""
    text = payload["text"]
    if text_stream is not None:
        chunks = []
        async for chunk in text_stream:
            chunks.append(chunk)
            yield sse("text_delta", {"delta": chunk})
        streamed = "".join(chunks).strip()
        if streamed:
            payload = {**payload, "text": normalize_inr(streamed)}
        # else: nothing streamed (Claude call failed) — keep the grounded fallback already in payload["text"]
    yield sse("message", payload)
    yield sse("done", {"ok":True})

@app.get("/health")
def health(): return {"status":"ok"}

@app.get("/health/database")
def database_health():
    db.rows("SELECT 1 AS ready")
    return {"status":"ok", "database":db.backend}

@app.post("/chat/storefront")
async def storefront_chat(body: ChatRequest, principal: Principal = Depends(require_customer)):
    customer_id = principal.id
    intent = shop.classify_intent(body.message)

    # Explicit checkout intent short-circuits: it reads the authoritative cart and
    # never reaches product search or cart addition (ADR 0021).
    if intent is Intent.CHECKOUT:
        staged_checkout = None
        try:
            staged_checkout = shop.stage_checkout(
                customer_id,
                reasoning=f'Customer requested checkout: "{body.message}"',
            )
            text = "Please review the verified cart, then continue to Razorpay when you're ready."
            why = "Checkout is staged for your confirmation. No order or payment link has been created yet."
        except ValueError as exc:
            text = f"I can't start checkout yet: {exc}."
            why = "Checkout is gated by the verified cart and the \u20b910,000 per-checkout bound."
        cart = shop.cart_read(customer_id)
        payload={"id":f"m_{hashlib.sha1((customer_id+body.message).encode()).hexdigest()[:10]}",
                 "role":"agent", "text":text, "why":why, "products":[],
                 "stagedCheckout":staged_checkout,
                 "cart":cart}
        return StreamingResponse(one_event("message",payload),media_type="text/event-stream")

    products = shop.search(customer_id, body.message, reasoning=f'Customer asked: "{body.message}"')
    added = None
    cart = shop.cart_read(customer_id)
    if intent is Intent.ADD_TO_CART:
        if not products and shop.is_relative_add_request(body.message):
            products = shop.last_search(customer_id)
        added, cart = shop.add_best_match(
            customer_id,
            products,
            reasoning=f'Customer asked to add an item: "{body.message}"',
        )
    upsell = None
    excluded_ids = {p["id"] for p in products} | {x["product_id"] for x in cart["lines"]}
    for line in cart["lines"]:
        candidate = shop.cross_sell(
            customer_id, line["product_id"],
            reasoning=f'One bounded catalog pairing for cart item {line["product_id"]}',
            excluded_ids=excluded_ids,
        )
        if candidate:
            upsell = {**candidate, "is_upsell": True}
            products.append(upsell)
            break
    text_stream = None
    if added:
        text = (
            f'Added "{added["name"]}" to your cart for {format_inr(added["price"])}. '
            f'Your cart subtotal is {format_inr(cart["total"])}.'
        )
    elif intent is Intent.ADD_TO_CART:
        text = "I couldn't add that because no in-stock matching product was found."
    else:
        text = f"I found {len(products)} in-stock catalog matches."  # grounded fallback if streaming fails outright
        text_stream = narrator.say_stream(
            "You are Cartisan's concise shopping assistant. Never invent products or prices. "
            "Every catalog amount is in INR; always use the \u20b9 symbol and never use $.",
            f"Customer: {body.message}\nVerified matches: {json.dumps([{'name':p['name'],'price':p['price'],'currency':'INR','is_upsell':p.get('is_upsell',False)} for p in products])}\n"
            "If one item is marked is_upsell, identify it as one optional pairing. Reply in at most 2 sentences.",
        )
    payload={"id":f"m_{hashlib.sha1((customer_id+body.message).encode()).hexdigest()[:10]}","role":"agent",
             "text":text,"why":"Only verified, in-stock catalog records were returned. Any is_upsell item is one code-bounded cross-sell for an item already in the cart.","products":products,
             "cart":cart}
    return StreamingResponse(streamed_message(payload,text_stream),media_type="text/event-stream")

@app.post("/chat/portal")
async def portal_chat(body: ChatRequest):
    snapshot=merchant.business_snapshot(body.conversation_id)
    catalog_context = [{"id":p["id"],"name":p["name"],"category":p["category"],"price":p["price"],"stock":p["stock"]}
                       for p in shop.products.values() if not p.get("options")]
    fallback = (
        f"The verified snapshot shows {format_inr(snapshot['sales'])} in sales across "
        f"{snapshot['orders']} paid orders. I did not queue a change because I could not safely form a proposal."
    )
    turn=await narrator.merchant_turn(
      f"Merchant request: {body.message}\nVerified snapshot: {json.dumps(snapshot)}\nVerified catalog: {json.dumps(catalog_context)}",
      fallback)
    proposal = turn.get("proposal")
    approval = None
    if proposal:
        try:
            kind,target_id,before,after,reasoning=merchant.validate_chat_proposal(proposal)
            approval=merchant.propose(body.conversation_id,kind,target_id,before,after,reasoning)
        except (TypeError,ValueError) as exc:
            audit.append(session_id=body.conversation_id,agent="merchant",action="proposal_rejected_by_guardrail",
                         reasoning="Model proposal failed a code-enforced bound",outcome="failed",gated=True,
                         result={"error":str(exc)})
    text=turn.get("reply") or "I reviewed the current snapshot."
    payload={"id":f"m_{hashlib.sha1((body.conversation_id+body.message).encode()).hexdigest()[:10]}","role":"agent",
             "text":text,"why":"Based on verified business and catalog data. Any proposed write is pending human approval; none was applied.",
             "approval":approval}
    return StreamingResponse(one_event("message",payload),media_type="text/event-stream")

@app.get("/catalog")
def catalog(): return [shop._public(p) for p in shop.products.values() if not p.get("variant_of")]
@app.get("/cart")
def cart(principal: Principal = Depends(require_customer)):
    return shop.cart_read(principal.id)

@app.post("/cart/items")
def add_cart(body: CartRequest, principal: Principal = Depends(require_customer)):
    try:
        return shop.add_to_cart(principal.id, body.product_id, body.quantity, body.reasoning,
                                expected_version=body.expected_version,
                                idempotency_key=body.idempotency_key)
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

@app.patch("/cart/items")
def update_cart(body: CartRequest, principal: Principal = Depends(require_customer)):
    try:
        return shop.update_quantity(principal.id, body.product_id, body.quantity, body.reasoning,
                                    expected_version=body.expected_version,
                                    idempotency_key=body.idempotency_key)
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

@app.delete("/cart/items/{product_id}")
def remove_cart(product_id: str, principal: Principal = Depends(require_customer)):
    try:
        return shop.remove_from_cart(principal.id, product_id)
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

@app.post("/checkout")
async def checkout(body: CheckoutRequest, principal: Principal = Depends(require_customer)):
    try:
        return await shop.checkout_handoff(principal.id, body.reasoning,
                                           expected_version=body.expected_version,
                                           idempotency_key=body.idempotency_key)
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

@app.get("/orders/{order_id}")
def order_status(order_id: str, principal: Principal = Depends(require_customer)):
    order = shop.order_status(principal.id, order_id)
    if not order:
        raise HTTPException(404, "Order not found")
    return order

@app.get("/me")
def me(principal: Principal = Depends(require_customer)):
    return {"id":principal.id,"email":principal.email,"role":principal.role,
            "display_name":principal.display_name}

@app.get("/portal/snapshot")
def snapshot(session_id: str): return merchant.business_snapshot(session_id)
@app.get("/portal/approvals")
def approvals(): return merchant.pending()
@app.post("/portal/approvals")
def propose(body: ProposalRequest): return merchant.propose(body.session_id,body.kind,body.target_id,body.before,body.after,body.reasoning)
@app.post("/portal/approvals/{change_id}/decision")
def decide(change_id: str,body: DecisionRequest):
    try:
        return merchant.decide(body.session_id,change_id,body.decision)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

@app.get("/audit")
def audit_list(agent: str | None=None,limit: int=200): return audit.list(agent=agent,limit=limit)

@app.post("/webhook/razorpay")
async def razorpay_webhook(request: Request,x_razorpay_signature: str=Header(default="")):
    raw=await request.body(); secret=os.getenv("RAZORPAY_WEBHOOK_SECRET","")
    expected=hmac.new(secret.encode(),raw,hashlib.sha256).hexdigest()
    if not secret or not hmac.compare_digest(expected,x_razorpay_signature): raise HTTPException(401,"Invalid signature")
    event=json.loads(raw); entity=event.get("payload",{}).get("payment_link",{}).get("entity",{})
    link_id=entity.get("id"); status=entity.get("status") or event.get("event","").split(".")[-1]
    if link_id:
        existing=db.rows("SELECT status,payload FROM orders WHERE payment_link_id=?",(link_id,))
        if existing and status in {"failed","cancelled","expired"} and existing[0]["status"] not in {"failed","cancelled","expired"}:
            # Stock was reserved when each line was added to the cart; a checkout that
            # never completes must give those units back, once, on this terminal transition.
            for line in json.loads(existing[0]["payload"]).get("lines",[]):
                db.execute("UPDATE products SET stock=stock+? WHERE id=?",(line["quantity"],line["product_id"]))
            shop.reload_products()
        db.execute("UPDATE orders SET status=? WHERE payment_link_id=?",(status,link_id))
    audit.append(session_id="webhook",agent="shopping",action="payment_status",reasoning="Verified Razorpay webhook",
                 outcome="failed" if status in {"failed","cancelled","expired"} else "ok",gated=True,
                 result={"payment_link_id":link_id,"status":status})
    return {"ok":True}
