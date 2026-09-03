from __future__ import annotations

import hashlib, hmac, json, os
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
from marketplace_backend.checkout import CheckoutRepository
from marketplace_backend.evidence import CommerceEventLog, EvidenceLedger, Inbox, Outbox
from marketplace_backend.identity import AuthenticationError, IdentityService, Principal
from marketplace_backend.inventory import InventoryRepository
from marketplace_backend.mcp_client import RazorpayMCPClient
from marketplace_backend.merchant_backend import MerchantBackend
from marketplace_backend.payments import (
    PaymentLinkDispatcher,
    WebhookProcessor,
    verify_signature,
)
from marketplace_backend.shopping import CheckoutRefused, ShoppingService
from marketplace_backend.store import Store
from marketplace_backend.storefront_backend import StorefrontBackend

from cartisan_agent import (
    CartisanAgentConfig,
    CartisanShoppingRuntime,
    CommerceServices,
    CoreCommercePort,
    PresentationLedger,
    SessionContext,
    SessionState,
    TurnStore,
)
from commerce_common.streaming import AgentEvent, to_sse

db = Store(
    path=os.getenv("CARTISAN_DB_PATH"),
    database_url=os.getenv("SUPABASE_DATABASE_URL"),
)
audit = AuditTrail(db)
identity = IdentityService(db)
shop = StorefrontBackend(db, audit)
merchant = MerchantBackend(db, audit, shop)
narrator = AgentNarrator()

# The Claude runtime (Phase 4). The shopping conversation runs on the Messages API loop
# in `cartisan_agent`; `marketplace_backend.routing` still decides checkout precedence,
# but it now steers that loop instead of standing in for it.
agent_config = CartisanAgentConfig()
ledger = EvidenceLedger(db)
inventory = InventoryRepository(db)
outbox, inbox = Outbox(db), Inbox(db)
checkout_repo = CheckoutRepository(db, inventory, ledger, outbox, CommerceEventLog(db))
core_port = CoreCommercePort(db, checkout=checkout_repo, config=agent_config)
commerce = CommerceServices(
    port=core_port,
    presentations=PresentationLedger(db, agent_config),
)

# Phase 5. The browser and the agent share `core_port`, so there is one cart, one
# price and one stock figure behind both. The payment half is host-only: the
# dispatcher asks Razorpay for a link, and the processor is the single path from a
# verified provider event to a paid order (ADR 0005, ADR 0011, ADR 0013).
def _gateway() -> RazorpayMCPClient:
    global _razorpay
    if _razorpay is None:
        _razorpay = RazorpayMCPClient()
    return _razorpay


_razorpay: RazorpayMCPClient | None = None


class _LazyGateway:
    """Defers credential loading to the first real payment, so the app still boots
    (and every non-payment test still runs) without Razorpay keys present."""

    async def create_payment_link(self, *, amount: int, reference_id: str, description: str) -> dict:
        return await _gateway().create_payment_link(
            amount=amount, reference_id=reference_id, description=description)


dispatcher = PaymentLinkDispatcher(db, checkout_repo, outbox, _LazyGateway(), ledger)
webhooks = WebhookProcessor(db, checkout_repo, inbox, ledger)
shopping = ShoppingService(db, core_port, checkout_repo, dispatcher)
turn_store = TurnStore(db, ledger)
shopping_agent = CartisanShoppingRuntime(
    services=commerce, store=db, config=agent_config, turns=turn_store
)

# The conversation transcript lives in this process. The turn state machine that
# reconnect and recovery need is durable (the `turns` table); a transcript that
# survives a restart is Phase 7's, with the audit surfaces.
_transcripts: dict[str, list[dict]] = {}
_states: dict[str, SessionState] = {}

app = FastAPI(title="Cartisan API", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=os.getenv("CORS_ORIGINS","http://localhost:3000").split(","),
                   allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

# Identity is never a request field. `conversation_id` groups a chat thread; it
# carries no authority, and the cart it reads is the authenticated customer's.
class ChatRequest(BaseModel):
    conversation_id: str = Field(default="default",min_length=1,max_length=100)
    message: str = Field(min_length=1,max_length=2000)

# Carts are variant-keyed. A variant is the thing that has a price, a stock level
# and an order line, so it is the only id the cart, the stage and the order share.
class CartRequest(BaseModel):
    variant_id: str
    quantity: int = Field(default=1,ge=0,le=10)
    reasoning: str = "Customer requested this cart change"
    expected_version: int | None = None
    idempotency_key: str | None = Field(default=None,max_length=200)

class StageRequest(BaseModel):
    fulfillment_option: str = Field(default="standard",max_length=40)
    note: str | None = Field(default=None,max_length=280)

class ConfirmRequest(BaseModel):
    stage_id: str
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


async def one_event(event: str, data: dict) -> AsyncIterator[str]:
    yield sse(event, data)
    yield sse("done", {"ok":True})


@app.get("/health")
def health(): return {"status":"ok"}

@app.get("/health/database")
def database_health():
    db.rows("SELECT 1 AS ready")
    return {"status":"ok", "database":db.backend}

def _conversation_key(principal: Principal, conversation_id: str) -> str:
    # Keyed by principal as well as conversation: a conversation id arriving from a
    # client carries no authority, and must never address another customer's transcript.
    return f"{principal.id}:{conversation_id}"


@app.post("/chat/storefront")
async def storefront_chat(body: ChatRequest, principal: Principal = Depends(require_customer)):
    """One agent turn, streamed. The events are `commerce_common.streaming.AgentEvent`
    types; a client renders the ones it knows and ignores the rest."""
    key = _conversation_key(principal, body.conversation_id)
    messages = _transcripts.setdefault(key, [])
    state = _states.setdefault(key, SessionState())
    session = SessionContext(conversation_id=key, customer_id=principal.id)
    messages.append({"role": "user", "content": body.message})

    async def stream() -> AsyncIterator[str]:
        try:
            async for event in shopping_agent.stream_turn(messages, session, state):
                yield to_sse(event)
        except Exception:  # the turn is already marked failed; the client gets one line
            yield to_sse(
                AgentEvent.error("Something went wrong on that turn. Please try again.")
            )
        yield sse("done", {"ok": True})

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.get("/chat/storefront/resume")
def storefront_resume(conversation_id: str, principal: Principal = Depends(require_customer)):
    """What a reconnecting client should show: the turn still running, or the reply it
    missed while it was away (ADR 0029)."""
    resumed = turn_store.resume(_conversation_key(principal, conversation_id))
    return resumed or {"state": "idle", "turn_id": None, "agent_message": None}


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
def catalog():
    """The normalized catalogue. Every buyable id here is a variant id."""
    return shopping.catalog()

@app.get("/cart")
async def cart(principal: Principal = Depends(require_customer)):
    return await shopping.cart(principal.id)

@app.post("/cart/items")
async def add_cart(body: CartRequest, principal: Principal = Depends(require_customer)):
    return await _cart_write(shopping.add(
        principal.id, body.variant_id, body.quantity,
        expected_version=body.expected_version, idempotency_key=body.idempotency_key))

@app.patch("/cart/items")
async def update_cart(body: CartRequest, principal: Principal = Depends(require_customer)):
    return await _cart_write(shopping.update(
        principal.id, body.variant_id, body.quantity,
        expected_version=body.expected_version, idempotency_key=body.idempotency_key))

@app.delete("/cart/items/{variant_id}")
async def remove_cart(variant_id: str, principal: Principal = Depends(require_customer)):
    return await _cart_write(shopping.remove(principal.id, variant_id))

async def _cart_write(coro):
    """A stale version is a 409 the client can recover from by re-reading; an item
    that cannot be sold is a 400. Neither is a 500, because neither is a surprise."""
    try:
        return await coro
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

# -- checkout ----------------------------------------------------------------
# Three separate calls, because they have three different authorities behind them.
# Staging holds nothing; confirmation is the customer's act and the only thing that
# reserves stock; the payment link is requested by the host, never by the model.

@app.post("/checkout/stage")
async def stage_checkout(body: StageRequest, principal: Principal = Depends(require_customer)):
    try:
        return await shopping.stage(principal.id, fulfillment_option=body.fulfillment_option,
                                    note=body.note)
    except CheckoutRefused as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

@app.post("/checkout/confirm")
async def confirm_checkout(body: ConfirmRequest, principal: Principal = Depends(require_customer)):
    try:
        return await shopping.confirm(principal.id, body.stage_id,
                                      idempotency_key=body.idempotency_key)
    except CheckoutRefused as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

@app.get("/orders")
def order_list(principal: Principal = Depends(require_customer)):
    return shopping.orders(principal.id)

@app.get("/orders/{order_id}")
def order_status(order_id: str, principal: Principal = Depends(require_customer)):
    try:
        return shopping.order(principal.id, order_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

@app.post("/orders/{order_id}/payment")
async def retry_payment(order_id: str, principal: Principal = Depends(require_customer)):
    """Try again on the same internal order. A retry is a new attempt, never a new
    order, so the stock the customer already holds is not reserved twice (ADR 0030)."""
    try:
        shopping.order(principal.id, order_id)  # ownership first, before any effect
        return await shopping.open_payment(principal.id, order_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except CheckoutRefused as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

@app.post("/orders/{order_id}/redirect")
def payment_redirect(order_id: str, principal: Principal = Depends(require_customer)):
    """The customer came back from Razorpay. That is not proof of payment: it moves
    the order to `payment_verification_pending` and waits for a verified event."""
    try:
        return shopping.redirect_returned(principal.id, order_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

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
async def razorpay_webhook(
    request: Request,
    x_razorpay_signature: str = Header(default=""),
    x_razorpay_event_id: str = Header(default=""),
):
    """The one path from a provider event to a paid order.

    The signature is checked against the exact bytes before the body is parsed, so
    an unsigned caller never reaches the commerce core at all. Past that, the
    processor stores the event once and applies it only if the order, amount,
    currency and provider reference all agree; anything else is quarantined with a
    reason and left for a human (ADR 0013, ADR 0024).
    """
    raw = await request.body()
    if not verify_signature(raw, x_razorpay_signature, os.getenv("RAZORPAY_WEBHOOK_SECRET", "")):
        raise HTTPException(401, "Invalid signature")
    try:
        event = json.loads(raw)
    except ValueError as exc:
        raise HTTPException(400, "Malformed webhook body") from exc
    if not isinstance(event, dict):
        raise HTTPException(400, "Malformed webhook body")
    # The provider's own delivery id is what deduplication keys on; it arrives in a
    # header, so it is folded in here rather than trusted from the body.
    if x_razorpay_event_id:
        event = {**event, "id": x_razorpay_event_id}
    outcome = webhooks.process(event)
    audit.append(session_id="webhook", agent="shopping", action="payment_status",
                 reasoning="Signed Razorpay webhook", gated=True,
                 outcome="ok" if outcome["result"] in {"applied", "duplicate", "ignored"} else "failed",
                 result=outcome)
    # A quarantine is still a 200: the delivery was received and recorded, and asking
    # the provider to redeliver an event we have already refused would not help.
    return {"ok": True, **outcome}


def require_operations_token(x_cartisan_ops_token: str = Header(default="")) -> None:
    """The maintenance endpoints below change commerce state, so they are not open.
    With no token configured they are closed rather than public."""
    expected = os.getenv("CARTISAN_OPS_TOKEN", "")
    if not expected or not hmac.compare_digest(expected, x_cartisan_ops_token):
        raise HTTPException(status_code=401, detail="Operations token required")


@app.post("/admin/expire", dependencies=[Depends(require_operations_token)])
def expire_abandoned():
    """Release what abandoned checkouts are holding. Idempotent, and host-triggered:
    no model-reachable path releases stock (ADR 0005)."""
    return checkout_repo.expire_unpaid()


@app.post("/admin/payments/drain", dependencies=[Depends(require_operations_token)])
async def drain_payment_outbox():
    """Deliver any payment-link request that an earlier provider failure left pending."""
    return await dispatcher.drain()
