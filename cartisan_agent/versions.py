"""Version stamps every turn records (ADR 0028).

`turns.prompt_version`, `turns.tool_contract_version` and `turns.skill_versions` are
not decoration: they say which bytes produced an answer, so a transcript evaluation
that passed can be tied to the prompt and contract it passed against. A change to the
static prompt text, a tool name or schema, or a skill body must bump the matching
constant here, and `tests/test_runtime_contracts.py` pins the contract so the bump is
deliberate rather than incidental.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable

PROMPT_VERSION = "shopping-prompt-2026.09.04"
MERCHANT_PROMPT_VERSION = "merchant-prompt-2026.09.04"
TOOL_CONTRACT_VERSION = "tools-2026.09.04"


def digest(parts: Iterable[str]) -> str:
    """A short stable digest of ordered text, for stamping skill bodies."""
    sha = hashlib.sha256()
    for part in parts:
        sha.update(part.encode("utf-8"))
        sha.update(b"\0")
    return sha.hexdigest()[:12]
