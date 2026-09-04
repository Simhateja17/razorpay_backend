"""The merchant components, and the provenance each one is joined from.

The rule is the same one the shopping side uses for cards: the model selects and
frames, the server supplies every fact. A metrics card renders the series
`query_metrics` returned, not the numbers in the call. A change preview renders
the `merchant_changes` row — its exact before and after documents, its status,
and the bounds it passed — not the model's summary of what it staged. A digest
line carries the kind of claim it is, and a kind that cannot be grounded is
refused (ADR 0017).

That is what makes "the operator approved exactly this" checkable afterwards:
the bytes on the approval surface came out of the record, so the record is what
they approved.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from commerce_common.presentation import (
    CHIPS_COMPONENT,
    CHIPS_TOOL,
    EnrichmentContext,
    PresentationComponent,
    PresentationPayload,
    PresentationRefused,
    PresentSuggestionsPayload,
)

from .merchant_gates import (
    CHANGE_PREVIEW_GATE,
    CLAIM_KIND_GATE,
    METRIC_PROVENANCE_GATE,
    change_preview_error,
    check_claim_kind,
    metric_provenance_error,
)
from .merchant_types import ClaimKind, MerchantSessionState
from .types import inr


def _state(context: EnrichmentContext) -> MerchantSessionState:
    return context.state


def _port(context: EnrichmentContext) -> Any:
    return context.backend.port


# -- payloads ----------------------------------------------------------------------


class DigestItem(PresentationPayload):
    heading: str
    body: str
    claim_kind: ClaimKind = "observed"


class DigestPayload(PresentationPayload):
    title: str
    items: list[DigestItem] = Field(min_length=1, max_length=8)


class MetricsPayload(PresentationPayload):
    title: str | None = None
    metric: str
    window_days: int
    reading: str


class ChangePreviewPayload(PresentationPayload):
    change_id: str
    note: str | None = None


# -- enrichment --------------------------------------------------------------------


async def _enrich_digest(payload: DigestPayload, context: EnrichmentContext) -> dict[str, Any]:
    state = _state(context)
    items: list[dict[str, Any]] = []
    for item in payload.items:
        refusal = check_claim_kind(f"{item.heading!r}", item.claim_kind, state)
        if refusal is not None:
            raise PresentationRefused(refusal, gate=CLAIM_KIND_GATE)
        items.append(item.model_dump())
    # The card names what the turn actually read, so a reader can go and check the
    # lines against the reads rather than taking the headings on trust.
    return {
        "title": payload.title,
        "items": items,
        "evidence": {
            "metrics_read": sorted(state.read_metrics),
            "claims_read": sorted(state.read_claims),
            "changes_staged": sorted(state.staged_changes),
        },
    }


async def _enrich_metrics(payload: MetricsPayload, context: EnrichmentContext) -> dict[str, Any]:
    state = _state(context)
    key = f"{payload.metric}:{payload.window_days}"
    series = state.read_metrics.get(key)
    if series is None:
        raise PresentationRefused(
            metric_provenance_error(payload.metric, payload.window_days, sorted(state.read_metrics)),
            gate=METRIC_PROVENANCE_GATE,
        )
    points = [point.model_dump(exclude_none=True) for point in series.points]
    return {
        "title": payload.title or f"{series.metric.replace('_', ' ').title()}, {series.window_days} days",
        "metric": series.metric,
        "window_days": series.window_days,
        "group_by": series.group_by,
        "unit": series.unit,
        "origins": series.origins,
        "points": points,
        "total": series.total,
        "total_label": inr(int(series.total)) if series.unit == "INR paise" and series.total is not None else None,
        "claim_kind": series.claim_kind,
        "basis": series.basis,
        "limitations": series.limitations,
        "reading": payload.reading,
    }


async def _enrich_change_preview(
    payload: ChangePreviewPayload, context: EnrichmentContext
) -> dict[str, Any]:
    state = _state(context)
    if payload.change_id not in state.staged_changes:
        raise PresentationRefused(
            change_preview_error(payload.change_id), gate=CHANGE_PREVIEW_GATE
        )
    # Re-read rather than render the copy held in session state: the operator may have
    # decided on it since it was staged, and the card must show the row as it stands.
    change = await _port(context).read_change(context.session, payload.change_id)
    if change is None:
        raise PresentationRefused(
            change_preview_error(payload.change_id), gate=CHANGE_PREVIEW_GATE
        )
    from marketplace_backend.merchant_changes import POLICY_BOUNDS

    return {
        "change_id": change.change_id,
        "kind": change.kind,
        "target_type": change.target_type,
        "target_id": change.target_id,
        "status": change.status,
        "before": change.before,
        "after": change.after,
        "rationale": change.rationale,
        "created_at": change.created_at,
        "policy_bounds": POLICY_BOUNDS.get(change.kind, {}),
        "note": payload.note,
        # Named on the card itself so the surface, not the prose, is what tells the
        # operator where a decision is made (ADR 0016).
        "decision_action": "host_decide_merchant_change",
        "approval_surface": context.config.approval_surface,
    }


MERCHANT_PRESENTATION_COMPONENTS: dict[str, PresentationComponent] = {
    "present_digest": PresentationComponent(
        name="present_digest",
        component="digest",
        payload_model=DigestPayload,
        enrich=_enrich_digest,
    ),
    "present_metrics": PresentationComponent(
        name="present_metrics",
        component="metrics",
        payload_model=MetricsPayload,
        enrich=_enrich_metrics,
    ),
    "present_change_preview": PresentationComponent(
        name="present_change_preview",
        component="change_preview",
        payload_model=ChangePreviewPayload,
        enrich=_enrich_change_preview,
    ),
    CHIPS_TOOL: PresentationComponent(
        name=CHIPS_TOOL,
        component=CHIPS_COMPONENT,
        payload_model=PresentSuggestionsPayload,
    ),
}
