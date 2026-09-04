from __future__ import annotations

import hmac, json, os
from pathlib import Path
from typing import AsyncIterator

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from marketplace_backend.carts import ConflictError
from marketplace_backend.checkout import CheckoutRepository
from marketplace_backend.evidence import (
    ORIGINS,
    CommerceEventLog,
    Correlation,
    EvidenceLedger,
    Inbox,
    Outbox,
)
from marketplace_backend.health import HealthMetrics
from marketplace_backend.observability import EvidenceView
from marketplace_backend.identity import AuthenticationError, IdentityService, Principal
from marketplace_backend.inventory import InventoryRepository
from marketplace_backend.mcp_client import RazorpayMCPClient
from marketplace_backend.merchant import DecisionRefused, MerchantService
from marketplace_backend.merchant_changes import MerchantChangeRepository
from marketplace_backend.metrics import MetricsRepository
from marketplace_backend.payments import (
    PaymentLinkDispatcher,
    WebhookProcessor,
    verify_signature,
)
from marketplace_backend.recovery import (
    RecoveryRefused,
    RecoveryService,
    order_recovery_actions,
)
from marketplace_backend.shopping import CheckoutRefused, ShoppingService
from cartisan_agent.outcomes import Unavailable
from marketplace_backend.store import Store

from cartisan_agent import (
    CartisanAgentConfig,
    CartisanMerchantRuntime,
    CartisanShoppingRuntime,
    CommerceServices,
    CoreCommercePort,
    CoreMerchantPort,
    MerchantAgentConfig,
    MerchantServices,
    MerchantSessionContext,
    MerchantSessionState,
    PresentationLedger,
    SessionContext,
    SessionState,
    TurnStore,
)
from commerce_common.streaming import AgentEvent, to_sse

from .lineage import CORRELATION_HEADER, DEMO_RUN_HEADER, request_correlation

db = Store(
    path=os.getenv("CARTISAN_DB_PATH"),
    database_url=os.getenv("SUPABASE_DATABASE_URL"),
)
identity = IdentityService(db)

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
shopping = ShoppingService(db, core_port, checkout_repo, dispatcher, ledger)

# Phase 7. Three readers and one set of controls, all on records that already
# existed and had no surface: the evidence ledger, the runtime's own counters, and
# the two stuck states the payment path can reach (ADR 0023, ADR 0030, ADR 0032).
evidence_view = EvidenceView(db)
# Not `health`: the /health route function below binds that name at import time
# and would shadow this, which is a 500 no test catches and one live call does.
health_metrics = HealthMetrics(db)
recovery = RecoveryService(db, checkout_repo, ledger)
turn_store = TurnStore(db, ledger)
shopping_agent = CartisanShoppingRuntime(
    services=commerce, store=db, config=agent_config, turns=turn_store
)

# Phase 6. The merchant agent runs the same loop over the same commerce core, and
# stops one step earlier: its writes create `pending` rows in `merchant_changes` and
# nothing else. `merchant_service` is the other side of that line — operator-only,
# in no tool list, and the only thing that turns an approval into a write (ADR 0016).
merchant_config = MerchantAgentConfig()
merchant_changes = MerchantChangeRepository(db, ledger)
merchant_port = CoreMerchantPort(
    db, changes=merchant_changes, metrics=MetricsRepository(db), config=merchant_config
)
merchant_service = MerchantService(db, merchant_port, merchant_changes, ledger)
merchant_agent = CartisanMerchantRuntime(
    services=MerchantServices(port=merchant_port), store=db, config=merchant_config,
    turns=turn_store,
)

# The model's message array lives in this process, and deliberately stays there: it
# holds tool_use/tool_result pairs that only the running turn can complete, and a
# half-written pair is exactly what makes the next request unanswerable.
#
# What a person needs back after a reload or a restart is not that array — it is
# what they asked and what they were told, and both are durable on `turns`. That is
# what `/chat/*/resume` returns, so a judge who restarts the backend mid-demo
# repaints the conversation instead of losing it.
_transcripts: dict[str, list[dict]] = {}
_states: dict[str, SessionState] = {}
_portal_transcripts: dict[str, list[dict]] = {}
_portal_states: dict[str, MerchantSessionState] = {}

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

# A decision names only the change and the verdict. Who decided comes from the
# verified operator principal, and what is being decided comes from the stored row —
# neither is a request field, because both are authority (ADR 0010, ADR 0016).
class DecisionRequest(BaseModel):
    decision: str = Field(pattern="^(approved|rejected)$")
    note: str | None = Field(default=None, max_length=400)

# A recovery action names the thing and the human's reason. Who acted comes from the
# operations token, not from the body.
class AcknowledgeRequest(BaseModel):
    note: str = Field(min_length=1, max_length=400)

class CancelOrderRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=400)


def require_customer(authorization: str | None = Header(default=None)) -> Principal:
    """Resolve the request's principal from a verified Supabase session."""
    try:
        principal = identity.principal(authorization)
    except AuthenticationError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    if principal.role != "customer":
        raise HTTPException(status_code=403, detail="This action requires a customer account")
    return principal

def require_operator(authorization: str | None = Header(default=None)) -> Principal:
    """The merchant surfaces act on the whole store, so they need an operator, not a
    signed-in shopper. The role comes from Supabase app metadata, never the client."""
    try:
        principal = identity.principal(authorization)
    except AuthenticationError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    if principal.role != "merchant_operator":
        raise HTTPException(status_code=403, detail="This action requires an operator account")
    return principal

def require_operations_token(x_cartisan_ops_token: str = Header(default="")) -> None:
    """The maintenance and recovery endpoints change commerce state, so they are not
    open. With no token configured they are closed rather than public, and none of
    them is reachable from a model (ADR 0005)."""
    expected = os.getenv("CARTISAN_OPS_TOKEN", "")
    if not expected or not hmac.compare_digest(expected, x_cartisan_ops_token):
        raise HTTPException(status_code=401, detail="Operations token required")


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data,ensure_ascii=False)}\n\n"


def _lineage_headers(correlation: Correlation) -> dict[str, str]:
    """A streamed response is constructed by the handler, so the header the
    dependency set on the injected `Response` never reaches the client. The client
    needs it to continue the journey on its next call, so it is set here too."""
    headers = {CORRELATION_HEADER: correlation.correlation_id}
    if correlation.demo_run_id:
        headers[DEMO_RUN_HEADER] = correlation.demo_run_id
    return headers


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


def _public_conversation_id(principal: Principal, conversation_key: str) -> str:
    """Return the client-side id without exposing the principal namespace."""
    prefix = f"{principal.id}:"
    return conversation_key.removeprefix(prefix)


def _conversation_summaries(principal: Principal, surface: str, limit: int) -> list[dict]:
    rows = db.rows(
        "SELECT c.id AS conversation_key, c.created_at, COUNT(t.id) AS turn_count, "
        "MAX(COALESCE(t.completed_at,t.started_at,c.created_at)) AS updated_at, "
        "(SELECT t2.user_message FROM turns t2 "
        "WHERE t2.conversation_id=c.id AND t2.user_message IS NOT NULL "
        "ORDER BY t2.sequence ASC LIMIT 1) AS title "
        "FROM conversations c LEFT JOIN turns t ON t.conversation_id=c.id "
        "WHERE c.principal_id=? AND c.surface=? "
        "GROUP BY c.id,c.created_at "
        "ORDER BY updated_at DESC,c.created_at DESC LIMIT ?",
        (principal.id, surface, max(1, min(limit, 100))),
    )
    return [
        {
            "conversation_id": _public_conversation_id(principal, row["conversation_key"]),
            "title": row["title"],
            "turn_count": int(row["turn_count"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        for row in rows
    ]


@app.post("/chat/storefront")
async def storefront_chat(body: ChatRequest, principal: Principal = Depends(require_customer),
                          correlation: Correlation = Depends(request_correlation)):
    """One agent turn, streamed. The events are `commerce_common.streaming.AgentEvent`
    types; a client renders the ones it knows and ignores the rest.

    The turn adopts the request's lineage, so the browser action that started it, the
    tools it calls and anything it stages are one journey rather than three (ADR 0032).
    """
    key = _conversation_key(principal, body.conversation_id)
    messages = _transcripts.setdefault(key, [])
    state = _states.setdefault(key, SessionState())
    session = SessionContext(conversation_id=key, customer_id=principal.id,
                             correlation_id=correlation.correlation_id,
                             demo_run_id=correlation.demo_run_id)
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

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers=_lineage_headers(correlation))


@app.get("/chat/storefront/conversations")
def storefront_conversations(limit: int = 50,
                             principal: Principal = Depends(require_customer)):
    """List this customer's durable shopping conversations, newest first.

    Conversation ids are namespaced by the verified principal in storage. The
    response removes that internal namespace before returning them, while the
    principal filter ensures the client cannot discover another customer's chats.
    """
    return _conversation_summaries(principal, "shopping", limit)


@app.get("/chat/storefront/resume")
def storefront_resume(conversation_id: str, principal: Principal = Depends(require_customer)):
    """What a reconnecting client should show: the turn still running, or the reply it
    missed while it was away — plus the conversation so far (ADR 0029).

    `history` comes from the `turns` table, so it survives a restart of this process.
    It is what a person said and what they were told, not the model's message array:
    that array holds tool_use blocks only the running turn can pair with results, and
    it stays where it is being written.
    """
    key = _conversation_key(principal, conversation_id)
    resumed = turn_store.resume(key) or {"state": "idle", "turn_id": None, "agent_message": None}
    return {**resumed, "history": turn_store.history(key)}


@app.get("/chat/portal/conversations")
def portal_conversations(limit: int = 50,
                         principal: Principal = Depends(require_operator)):
    """List this operator's durable merchant conversations, newest first."""
    return _conversation_summaries(principal, "merchant", limit)


@app.post("/chat/portal")
async def portal_chat(body: ChatRequest, principal: Principal = Depends(require_operator),
                      correlation: Correlation = Depends(request_correlation)):
    """One merchant agent turn, streamed.

    The same `AgentEvent` stream the storefront speaks, so the portal renders tool
    calls, components and errors the same way. A turn may stage a change, which
    arrives as a `change_update` event and appears in the approval queue; it cannot
    approve or apply one, and there is no tool on this surface that could.
    """
    key = _conversation_key(principal, body.conversation_id)
    messages = _portal_transcripts.setdefault(key, [])
    state = _portal_states.setdefault(key, MerchantSessionState())
    session = MerchantSessionContext(conversation_id=key, customer_id=principal.id,
                                     correlation_id=correlation.correlation_id,
                                     demo_run_id=correlation.demo_run_id)
    messages.append({"role": "user", "content": body.message})

    async def stream() -> AsyncIterator[str]:
        try:
            async for event in merchant_agent.stream_turn(messages, session, state):
                yield to_sse(event)
        except Exception:  # the turn is already marked failed; the client gets one line
            yield to_sse(
                AgentEvent.error("Something went wrong on that turn. Please try again.")
            )
        yield sse("done", {"ok": True})

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers=_lineage_headers(correlation))


@app.get("/chat/portal/resume")
def portal_resume(conversation_id: str, principal: Principal = Depends(require_operator)):
    """What a reconnecting portal should show: the turn still running, or the reply it
    missed while it was away, plus the durable conversation so far (ADR 0029)."""
    key = _conversation_key(principal, conversation_id)
    resumed = turn_store.resume(key) or {"state": "idle", "turn_id": None, "agent_message": None}
    return {**resumed, "history": turn_store.history(key)}


@app.get("/catalog")
def catalog():
    """The normalized catalogue. Every buyable id here is a variant id."""
    return shopping.catalog()

@app.get("/cart")
async def cart(principal: Principal = Depends(require_customer)):
    return await shopping.cart(principal.id)

@app.post("/cart/items")
async def add_cart(body: CartRequest, principal: Principal = Depends(require_customer),
                   correlation: Correlation = Depends(request_correlation)):
    return await _cart_write(shopping.add(
        principal.id, body.variant_id, body.quantity,
        expected_version=body.expected_version, idempotency_key=body.idempotency_key,
        correlation=correlation))

@app.patch("/cart/items")
async def update_cart(body: CartRequest, principal: Principal = Depends(require_customer),
                      correlation: Correlation = Depends(request_correlation)):
    return await _cart_write(shopping.update(
        principal.id, body.variant_id, body.quantity,
        expected_version=body.expected_version, idempotency_key=body.idempotency_key,
        correlation=correlation))

@app.delete("/cart/items/{variant_id}")
async def remove_cart(variant_id: str, principal: Principal = Depends(require_customer),
                      correlation: Correlation = Depends(request_correlation)):
    return await _cart_write(shopping.remove(principal.id, variant_id, correlation=correlation))

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
async def stage_checkout(body: StageRequest, principal: Principal = Depends(require_customer),
                         correlation: Correlation = Depends(request_correlation)):
    try:
        return await shopping.stage(principal.id, fulfillment_option=body.fulfillment_option,
                                    note=body.note, correlation=correlation)
    except CheckoutRefused as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

@app.post("/checkout/confirm")
async def confirm_checkout(body: ConfirmRequest, principal: Principal = Depends(require_customer),
                           correlation: Correlation = Depends(request_correlation)):
    try:
        return await shopping.confirm(principal.id, body.stage_id,
                                      idempotency_key=body.idempotency_key,
                                      correlation=correlation)
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

# -- the merchant surface ----------------------------------------------------
# Reads are open to any operator; decisions are the operator's own act, recorded
# against their verified principal. Nothing below is reachable from a tool.

@app.get("/portal/snapshot")
async def snapshot(window_days: int = 7, principal: Principal = Depends(require_operator)):
    return await merchant_service.snapshot(principal.id, window_days)


@app.get("/portal/metrics")
async def portal_metrics(metric: str, window_days: int = 30, group_by: str | None = None,
                         principal: Principal = Depends(require_operator)):
    try:
        return await merchant_service.metrics(principal.id, metric, window_days, group_by)
    except Unavailable as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/portal/changes")
def portal_changes(limit: int = 50, principal: Principal = Depends(require_operator)):
    """The approval queue: what is waiting, and what was decided, each with the exact
    before-and-after documents the agent staged."""
    return merchant_service.changes_list(limit=min(limit, 200))


@app.get("/portal/changes/{change_id}")
def portal_change(change_id: str, principal: Principal = Depends(require_operator)):
    try:
        return merchant_service.change(change_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/portal/changes/{change_id}/decision")
def portal_decide(change_id: str, body: DecisionRequest,
                  principal: Principal = Depends(require_operator)):
    """The operator's decision, and — on an approval — the application that follows it.

    Cartisan re-reads the record and re-checks the bounds here, before writing. A
    proposal whose target moved since it was staged, or whose bounds no longer hold
    against current figures, is refused with the reason: the approval stands in the
    ledger, the change is marked failed, and nothing was written (ADR 0016).
    """
    try:
        return merchant_service.decide(
            operator_id=principal.id, change_id=change_id, decision=body.decision,
            note=body.note)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except DecisionRefused as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


# -- evidence -----------------------------------------------------------------
# The evidence ledger, which until now had no reader. These replace `/audit` and
# the flat `audit` table behind it: that table had one row per action with no
# principal filter, no correlation, no origin and no actor type, so every session's
# rows arrived in one undifferentiated list — the "unrelated-session noise" this
# phase has to rule out. `evidence_records` already held all four (ADR 0023).

@app.get("/evidence")
def my_evidence(demo_run_id: str | None = None, correlation_id: str | None = None,
                outcome: str | None = None, limit: int = 100,
                principal: Principal = Depends(require_customer)):
    """A customer's own evidence. The principal filter is applied from the verified
    token and is not a parameter, so this endpoint cannot be widened by asking."""
    try:
        return evidence_view.records(
            actor_id=principal.id, demo_run_id=demo_run_id, correlation_id=correlation_id,
            outcome=outcome, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/evidence/journeys/{correlation_id}")
def my_journey(correlation_id: str, principal: Principal = Depends(require_customer)):
    """One of the customer's own journeys, end to end.

    Ownership is checked against the ledger before the journey is assembled: a
    correlation id is a handle a client can guess at, so it grants nothing on its
    own. A journey the customer has no row in does not exist to them.
    """
    if not evidence_view.records(actor_id=principal.id, correlation_id=correlation_id, limit=1):
        raise HTTPException(status_code=404, detail="No such journey")
    return evidence_view.journey(correlation_id)


@app.get("/portal/evidence")
def portal_evidence(actor_id: str | None = None, demo_run_id: str | None = None,
                    correlation_id: str | None = None, origin: str | None = None,
                    surface: str | None = None, outcome: str | None = None,
                    actor_type: str | None = None, action: str | None = None,
                    target_id: str | None = None, since: str | None = None,
                    limit: int = 100, principal: Principal = Depends(require_operator)):
    """The store-wide ledger, filtered. An operator acts on the whole store, so this
    one takes a principal as a *filter* rather than forcing their own."""
    try:
        return evidence_view.records(
            actor_id=actor_id, demo_run_id=demo_run_id, correlation_id=correlation_id,
            origin=origin, surface=surface, outcome=outcome, actor_type=actor_type,
            action=action, target_id=target_id, since=since, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/portal/evidence/filters")
def portal_evidence_filters(demo_run_id: str | None = None,
                            principal: Principal = Depends(require_operator)):
    """What is actually in the ledger to filter by — the demo runs recorded and the
    action names present, so the UI offers what exists rather than a fixed list."""
    return {"demo_runs": evidence_view.demo_runs(), "origins": list(ORIGINS),
            "actions": evidence_view.actions(demo_run_id=demo_run_id)}


@app.get("/portal/evidence/journeys")
def portal_journeys(actor_id: str | None = None, demo_run_id: str | None = None,
                    origin: str | None = None, limit: int = 40,
                    principal: Principal = Depends(require_operator)):
    """One row per lineage: who started it, what it produced, and how it ended."""
    try:
        return evidence_view.journeys(actor_id=actor_id, demo_run_id=demo_run_id,
                                      origin=origin, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/portal/evidence/journeys/{correlation_id}")
def portal_journey(correlation_id: str, principal: Principal = Depends(require_operator)):
    """One journey from the customer's request to the Razorpay evidence: the turns,
    the tool calls, the order, its payment attempts, and the provider's own answer —
    including a refused one — in the order they happened."""
    journey = evidence_view.journey(correlation_id)
    if not journey["found"]:
        raise HTTPException(status_code=404, detail="No such journey")
    return journey


@app.get("/portal/health")
def portal_health(hours: int = 24, demo_run_id: str | None = None,
                  principal: Principal = Depends(require_operator)):
    """Production health, every figure carrying the formula that produced it and the
    window it covers — the same `Claim` shape the merchant metrics use (ADR 0017)."""
    return health_metrics.report(hours=hours, demo_run_id=demo_run_id)


# -- payment recovery ---------------------------------------------------------
# Reading what is stuck needs an operator; changing it needs the operations token,
# exactly like `/admin/expire`. Nothing here is in any tool list, and no
# model-reachable path reaches it (ADR 0005, ADR 0030).

@app.get("/portal/recovery")
def recovery_queue(limit: int = 50, principal: Principal = Depends(require_operator)):
    """Dead-lettered effects, quarantined provider events, events never decided, and
    orders holding stock with nothing in flight — each with the reason and the
    actions still open to it."""
    return recovery.queue(limit=limit)


@app.post("/admin/recovery/messages/{message_id}/retry",
          dependencies=[Depends(require_operations_token)])
def recovery_retry_message(message_id: str):
    """Return a dead-lettered payment-link request to the queue. The effect is
    idempotent per attempt, so this recovers the existing link rather than making a
    second one."""
    try:
        return recovery.retry_message(message_id)
    except RecoveryRefused as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/admin/recovery/events/{inbox_id}/acknowledge",
          dependencies=[Depends(require_operations_token)])
def recovery_acknowledge(inbox_id: str, body: AcknowledgeRequest):
    """Record that a human read a quarantined event and what they concluded.

    The event stays quarantined. A payload that failed verification is never
    re-applied, because a wrong `paid` is the worst thing this system can produce;
    the recovery is on the order, not on the payload (ADR 0013).
    """
    try:
        return recovery.acknowledge(inbox_id, note=body.note)
    except RecoveryRefused as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/admin/recovery/events/{inbox_id}/reprocess",
          dependencies=[Depends(require_operations_token)])
def recovery_reprocess(inbox_id: str):
    """Re-run an event that was stored but never decided, through the same
    verification a live delivery gets. A payload that does not match is quarantined
    now rather than sitting undecided forever."""
    try:
        return recovery.reprocess_event(inbox_id, webhooks)
    except RecoveryRefused as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/admin/recovery/orders/{order_id}/cancel",
          dependencies=[Depends(require_operations_token)])
def recovery_cancel_order(order_id: str, body: CancelOrderRequest):
    """Give up on an unpaid order and release the stock it holds."""
    try:
        return recovery.cancel_order(order_id, reason=body.reason)
    except RecoveryRefused as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


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
    # The processor writes the evidence for this event itself, carrying the lineage
    # it recovered from the attempt. The flat `audit` row that used to be appended
    # here as well is gone with the table: it had no correlation, no origin and no
    # actor type, so it could only ever restate what the ledger already knew.
    # A quarantine is still a 200: the delivery was received and recorded, and asking
    # the provider to redeliver an event we have already refused would not help.
    return {"ok": True, **outcome}


@app.post("/admin/expire", dependencies=[Depends(require_operations_token)])
def expire_abandoned():
    """Release what abandoned checkouts are holding. Idempotent, and host-triggered:
    no model-reachable path releases stock (ADR 0005)."""
    return checkout_repo.expire_unpaid()


@app.post("/admin/payments/drain", dependencies=[Depends(require_operations_token)])
async def drain_payment_outbox():
    """Deliver any payment-link request that an earlier provider failure left pending."""
    return await dispatcher.drain()
