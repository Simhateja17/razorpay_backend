"""Cartisan's Claude runtime.

What lives here: the tool contracts the architecture fixes, the adapted prompts,
the Messages API turn loop, skill loading, prompt caching, persisted turns and
tool executions, typed outcomes, and the gates each surface enforces.

Two surfaces sit on one loop (`runtime.AgentRuntime`). Shopping reaches as far as
an expiring checkout preview and no further (ADR 0015). The merchant surface
reaches as far as a `pending` staged change and no further (ADR 0016): there is
no apply, approve, reject, price, refund, or campaign-send tool on it, and
`marketplace_backend.merchant` — which is not model-reachable at all — is what
turns an operator's approval into a write.
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
from .merchant_core_port import CoreMerchantPort
from .merchant_executor import MerchantServices, MerchantToolExecutor
from .merchant_loop import CartisanMerchantRuntime
from .merchant_ports import MerchantPort
from .merchant_types import (
    Claim,
    MerchantSessionContext,
    MerchantSessionState,
    StagedChange,
)
from .outcomes import BusinessRefusal, Conflict, Outcome, Unavailable
from .ports import CommercePort
from .presentations import PresentationLedger
from .runtime import AgentRuntime
from .turns import TurnInProgress, TurnStore
from .types import SessionContext, SessionState
from .versions import MERCHANT_PROMPT_VERSION, PROMPT_VERSION, TOOL_CONTRACT_VERSION

__all__ = [
    "FORBIDDEN_TOOLS",
    "MERCHANT_PRESENTATION",
    "MERCHANT_PROMPT_VERSION",
    "MERCHANT_READS",
    "MERCHANT_STAGING",
    "PROMPT_VERSION",
    "SHOPPING_MUTATIONS",
    "SHOPPING_PRESENTATION",
    "SHOPPING_READS",
    "TOOL_CONTRACT_VERSION",
    "AgentRuntime",
    "BusinessRefusal",
    "CartisanAgentConfig",
    "CartisanMerchantRuntime",
    "CartisanShoppingRuntime",
    "CartisanToolExecutor",
    "Claim",
    "CommercePort",
    "CommerceServices",
    "Conflict",
    "CoreCommercePort",
    "CoreMerchantPort",
    "MerchantAgentConfig",
    "MerchantPort",
    "MerchantServices",
    "MerchantSessionContext",
    "MerchantSessionState",
    "MerchantToolExecutor",
    "Outcome",
    "PresentationLedger",
    "SessionContext",
    "SessionState",
    "StagedChange",
    "TurnInProgress",
    "TurnStore",
    "Unavailable",
    "build_merchant_tools",
    "build_shopping_tools",
    "tool_names",
]
