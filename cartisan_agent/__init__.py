"""Cartisan's Claude runtime (Phase 4).

What lives here: the tool contracts the architecture fixes, the adapted prompts, the
Messages API turn loop, skill loading, prompt caching, persisted turns and tool
executions, typed outcomes, and the grounding, presentation-reference, checkout-
precedence, and forbidden-capability gates.

What does not: the shopping tools' full behaviour. Their contracts and gates are
settled here and their bodies are deliberately thin — reservation, internal orders, the
Razorpay handoff, and verified payment are Phase 5's, and the merchant runtime is
Phase 6's, though its contracts are fixed here too so the cached prefix and the
evaluations have something stable to hold.
"""

from .config import FORBIDDEN_TOOLS, CartisanAgentConfig, MerchantAgentConfig
from .contracts import (
    MERCHANT_PRESENTATION,
    MERCHANT_READS,
    MERCHANT_STAGING,
    SHOPPING_MUTATIONS,
    SHOPPING_PRESENTATION,
    SHOPPING_READS,
    build_merchant_tools,
    build_shopping_tools,
    tool_names,
)
from .core_port import CoreCommercePort
from .executor import CartisanToolExecutor, CommerceServices
from .loop import CartisanShoppingRuntime
from .outcomes import BusinessRefusal, Conflict, Outcome, Unavailable
from .ports import CommercePort
from .presentations import PresentationLedger
from .turns import TurnInProgress, TurnStore
from .types import SessionContext, SessionState
from .versions import PROMPT_VERSION, TOOL_CONTRACT_VERSION

__all__ = [
    "FORBIDDEN_TOOLS",
    "MERCHANT_PRESENTATION",
    "MERCHANT_READS",
    "MERCHANT_STAGING",
    "PROMPT_VERSION",
    "SHOPPING_MUTATIONS",
    "SHOPPING_PRESENTATION",
    "SHOPPING_READS",
    "TOOL_CONTRACT_VERSION",
    "BusinessRefusal",
    "CartisanAgentConfig",
    "CartisanShoppingRuntime",
    "CartisanToolExecutor",
    "CommercePort",
    "CommerceServices",
    "Conflict",
    "CoreCommercePort",
    "MerchantAgentConfig",
    "Outcome",
    "PresentationLedger",
    "SessionContext",
    "SessionState",
    "TurnInProgress",
    "TurnStore",
    "Unavailable",
    "build_merchant_tools",
    "build_shopping_tools",
    "tool_names",
]
