"""The gates a merchant tool call passes before it reaches the port.

Each one answers a clause of this phase's acceptance criterion — "every approval
or refusal has exact evidence lineage" — by refusing anything that cannot show
where it came from:

* **Claim kind** (ADR 0017). `causal` is refused outright, because Cartisan has
  run no experiment and holds no causal evidence, so there is nothing a causal
  claim could be grounded in. `observed` needs a read this session; `estimated`
  needs a deterministic estimate this session produced.
* **Listing provenance.** A staging tool takes only a product or variant id a
  catalogue read returned this session, so a change cannot be proposed against a
  record the model has not looked at.
* **Metric provenance.** `present_metrics` names a metric and window
  `query_metrics` actually returned, and the card renders from that record rather
  than from numbers in the call.
* **Change preview.** `present_change_preview` names a change this session staged
  or read, and the card is rendered from the row, not from the model's summary of
  it.
* **Forbidden capability.** Shared with shopping: applying, approving, refunding
  or pricing are not this agent's, on any surface (ADR 0016).

There is no gate here that lets a change through to `applied`, because there is
no tool that could ask for it.
"""

from __future__ import annotations

from typing import Any

from commerce_common.grounding import GroundingRule, matches_terms_and_cues

from .config import MerchantAgentConfig
from .merchant_types import CAUSAL, MerchantSessionState

CLAIM_KIND_GATE = "claim_kind"
LISTING_PROVENANCE_GATE = "listing_provenance"
METRIC_PROVENANCE_GATE = "metric_provenance"
CHANGE_PREVIEW_GATE = "change_preview"
MERCHANT_POLICY_GATE = "merchant_policy"

# Words that mean the operator is asking how the business is doing. A turn matching
# these reads before it describes, exactly as a shopping turn searches before it
# recommends.
PERFORMANCE_TERMS: tuple[str, ...] = (
    "sales", "revenue", "orders", "performance", "doing", "trend", "growth", "week",
    "month", "conversion", "aov", "refunds", "numbers", "figures", "business",
)
PERFORMANCE_CUES: tuple[str, ...] = (
    "?", "how", "what", "why", "show", "tell", "give", "compare", "since", "last",
    "this", "any", "which",
)


def causal_claim_error(what: str) -> str:
    return (
        f"{what} was marked as a causal claim. Cartisan holds no causal evidence: no "
        "experiment has run, and nothing in the records shows that one thing caused "
        "another. Report it as observed if a read measured it, as estimated if a "
        "formula produced it from measured inputs, and describe the relationship as a "
        "relationship."
    )


def unread_claim_error(kind: str) -> str:
    if kind == "estimated":
        return (
            "An estimated figure has to come from an estimate this turn computed — the "
            "days of cover on an inventory alert, the bounds in a pricing context. Call "
            "the read that produces it, then state the figure it returned."
        )
    return (
        "An observed figure has to come from a read in this conversation. Call "
        "get_business_snapshot or query_metrics first, then say what it returned."
    )


def listing_provenance_error(name: str, target: str) -> str:
    return (
        f"{name} names {target}, which no catalogue read returned in this conversation. "
        "Find it with search_listings or open it with get_listing, then stage the change "
        "against the id and the current values that read returned. Nothing was staged."
    )


def metric_provenance_error(metric: str, window_days: int, available: list[str]) -> str:
    known = ", ".join(available) if available else "none yet"
    return (
        f"No query_metrics result for {metric} over {window_days} days exists in this "
        f"conversation, so there is no series to render (read this turn: {known}). Call "
        "query_metrics with exactly that metric and window, then present it."
    )


def change_preview_error(change_id: str) -> str:
    return (
        f"{change_id} is not a change staged or read in this conversation. Preview a "
        "change you staged this turn, or one get_pending_changes returned."
    )


def check_claim_kind(what: str, kind: str, state: MerchantSessionState) -> str | None:
    """The refusal reason for a claim kind that cannot be grounded, or None."""
    if kind == CAUSAL:
        return causal_claim_error(what)
    if kind == "observed" and not (state.read_metrics or state.read_claims):
        return unread_claim_error("observed")
    if kind == "estimated" and not any(
        claim.claim_kind == "estimated" for claim in state.read_claims.values()
    ):
        return unread_claim_error("estimated")
    return None


# -- grounding rules, in precedence order ------------------------------------------


def _performance(
    config: MerchantAgentConfig, text: str, _: MerchantSessionState
) -> dict[str, Any] | None:
    fires = config.performance_grounding_gate and matches_terms_and_cues(
        text, PERFORMANCE_TERMS, PERFORMANCE_CUES
    )
    return {} if fires else None


MERCHANT_GROUNDING_RULES: tuple[GroundingRule, ...] = (
    GroundingRule("performance", "get_business_snapshot", _performance),
)


def is_performance_turn(config: MerchantAgentConfig, text: str) -> bool:
    return bool(
        config.performance_grounding_gate
        and matches_terms_and_cues(text, PERFORMANCE_TERMS, PERFORMANCE_CUES)
    )
