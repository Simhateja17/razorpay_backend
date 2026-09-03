from __future__ import annotations

import os
import logging
import json
from typing import Any
from anthropic import AsyncAnthropic

logger = logging.getLogger(__name__)


class AgentNarrator:
    """Claude supplies language and reasoning; code-owned backends supply all facts/actions."""
    def __init__(self) -> None:
        workspace_id = os.getenv("ANTHROPIC_WORKSPACE_ID")
        headers = {"anthropic-workspace-id": workspace_id} if workspace_id else None
        self.client = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"), default_headers=headers)
        self.model = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5")
        self.effort = os.getenv("ANTHROPIC_EFFORT", "low")

    async def say(self, system: str, prompt: str, fallback: str) -> str:
        try:
            message = await self.client.messages.create(model=self.model,max_tokens=180,
                                                        output_config={"effort": self.effort},
                                                        system=system,messages=[{"role":"user","content":prompt}])
            return "".join(block.text for block in message.content if block.type == "text").strip() or fallback
        except Exception:
            logger.exception("Claude narration failed; using grounded fallback")
            return fallback

    async def merchant_turn(self, prompt: str, fallback: str) -> dict[str, Any]:
        """Return merchant wording plus at most one proposed write; never apply it here."""
        schema = {
            "type": "object", "additionalProperties": False,
            "properties": {
                "reply": {"type": "string"},
                "proposal": {
                    "anyOf": [
                        {"type": "null"},
                        {"type": "object", "additionalProperties": False,
                         "properties": {
                             "kind": {"type": "string", "enum": ["price_update", "restock", "pause_product", "activate_product", "promotion", "content_edit"]},
                             "target_id": {"type": ["string", "null"]},
                             "after": {"type": "object"},
                             "reasoning": {"type": "string"},
                         }, "required": ["kind", "target_id", "after", "reasoning"]},
                    ]
                },
            }, "required": ["reply", "proposal"],
        }
        try:
            message = await self.client.messages.create(
                model=self.model, max_tokens=500,
                output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": schema}},
                system=("You are Cartisan's merchant agent. Use only supplied facts. Propose at most one write, "
                        "and only when the merchant explicitly asks for a recommendation or change. A proposal is "
                        "never applied automatically; clearly say it was queued for human approval. Return null "
                        "when the request is informational or evidence is insufficient."),
                messages=[{"role": "user", "content": prompt}],
            )
            text = "".join(block.text for block in message.content if block.type == "text")
            return json.loads(text)
        except Exception:
            logger.exception("Claude merchant decision failed; no change will be proposed")
            return {"reply": fallback, "proposal": None}
