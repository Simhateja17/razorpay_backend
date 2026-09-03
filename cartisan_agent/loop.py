"""The shopping agent's turn loop on the Messages API (ADR 0014).

One model call per round, tools dispatched as their content blocks close, a rolling
cache breakpoint through the conversation, and the turn ending on a round of clean
presentation calls. Adapted from the checked-in reference runtime; what is Cartisan's is
what surrounds the loop — deterministic checkout precedence ahead of the first round,
the turn and tool-execution rows every call writes, and the typed outcomes those rows
carry.

    runtime = CartisanShoppingRuntime(services=..., store=..., skills_dir=...)
    async for event in runtime.stream_turn(messages, session, state):
        ...

`messages` ends with the user's message and is extended in place with the turn's
assistant messages and tool results, so the host stores it as it stands. `state` is the
conversation's provenance and comes back on every turn.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

from anthropic import AsyncAnthropic

from commerce_common.grounding import first_forced_tool
from commerce_common.memory import MemoryRuntime
from commerce_common.presentation import partial_ui_tool_names
from commerce_common.prompt_assembly import (
    build_request_messages,
    build_system_blocks,
    with_eager_input,
    with_tool_cache_control,
)
from commerce_common.skills import SkillRegistry
from commerce_common.streaming import AgentEvent, ToolOutcome
from commerce_common.turn import (
    EagerDispatcher,
    StreamedRound,
    accumulate_usage,
    assistant_message,
    close_open_tool_uses,
    compact_history,
    elapsed_ms,
    latest_user_text,
    log_model_call,
    outcome_events,
    prompt_tokens,
    round_closes_turn,
    salvage_round,
    tool_result_block,
    transcript_text,
    usage_totals,
)
from marketplace_backend.store import Store

from .config import CartisanAgentConfig
from .contracts import build_shopping_tools
from .executor import CartisanToolExecutor, CommerceServices, build_memory
from .gates import GROUNDING_RULES, is_checkout_turn
from .outcomes import classify
from .presentations import PRESENTATION_COMPONENTS
from .prompts import build_dynamic_context, build_static_system, prompt_fingerprint
from .turns import TurnInProgress, TurnRecord, TurnStore, conversation_lock
from .types import SessionContext, SessionState
from .versions import PROMPT_VERSION, TOOL_CONTRACT_VERSION, digest

logger = logging.getLogger(__name__)

SKILLS_DIR = Path(__file__).resolve().parent / "skills"


class CartisanShoppingRuntime:
    def __init__(
        self,
        *,
        services: CommerceServices,
        store: Store,
        config: CartisanAgentConfig | None = None,
        skills: SkillRegistry | None = None,
        skills_dir: Path | None = SKILLS_DIR,
        client: AsyncAnthropic | None = None,
        memory: MemoryRuntime | None = None,
        turns: TurnStore | None = None,
    ) -> None:
        self.config = config or CartisanAgentConfig()
        self.services = services
        self.skills = skills or (
            SkillRegistry.from_dir(skills_dir)
            if skills_dir and skills_dir.exists()
            else SkillRegistry([])
        )
        # An identity-linked API key must name the workspace it acts in, or every
        # request is rejected with a 400 before the turn starts. `AgentNarrator` has
        # always sent this header; the runtime has to send it too.
        workspace_id = os.getenv("ANTHROPIC_WORKSPACE_ID")
        self.client = client or AsyncAnthropic(
            timeout=self.config.request_timeout_s,
            default_headers={"anthropic-workspace-id": workspace_id} if workspace_id else None,
        )
        self.memory = memory or build_memory(self.config)
        self.turns = turns or TurnStore(store)

        # Built once: the same bytes on every request of this deployment, which is what
        # makes them the cacheable prefix (ADR 0028).
        self._static_system = build_static_system(self.config, self.skills)
        self._specs = PRESENTATION_COMPONENTS
        self._partial_ui_tools = partial_ui_tool_names(PRESENTATION_COMPONENTS, ())
        self._tools = with_tool_cache_control(
            with_eager_input(
                build_shopping_tools(self.config, self.skills.names), self._partial_ui_tools
            )
        )
        self.prompt_fingerprint = prompt_fingerprint(self._static_system, self._tools)
        self.skill_versions = json.dumps(
            [
                f"{name}@{digest([self.skills.get_instructions(name) or ''])}"
                for name in self.skills.names
            ]
        )

    # -- one turn -------------------------------------------------------------

    async def stream_turn(
        self,
        messages: list[dict[str, Any]],
        session: SessionContext,
        state: SessionState | None = None,
    ) -> AsyncIterator[AgentEvent]:
        state = state if state is not None else SessionState()
        turn_started = time.monotonic()
        user_text = latest_user_text(messages)

        # Checkout precedence is decided here, in code, before the model is asked
        # anything at all (ADR 0021). It pins the first round and, through the gate in
        # the executor, closes search and cart mutation for the whole turn.
        state.checkout_turn = is_checkout_turn(self.config, user_text)

        async with conversation_lock(session.conversation_id):
            try:
                turn = self.turns.begin(
                    session,
                    user_message=user_text,
                    prompt_version=f"{PROMPT_VERSION}+{self.prompt_fingerprint}",
                    tool_contract_version=TOOL_CONTRACT_VERSION,
                    skill_versions=self.skill_versions,
                    skill_names=self.skills.names,
                )
            except TurnInProgress as busy:
                yield AgentEvent.error(
                    "This conversation already has a turn in progress; reconnecting to it."
                )
                yield AgentEvent.turn_complete("in_progress", usage_totals(), 0, 0)
                logger.info("turn already live for %s: %s", session.conversation_id, busy.turn_id)
                return
            try:
                async for event in self._run(messages, session, state, turn, turn_started):
                    yield event
            except Exception as failure:
                self.turns.fail(turn, f"{type(failure).__name__}: {failure}")
                raise

    async def _run(
        self,
        messages: list[dict[str, Any]],
        session: SessionContext,
        state: SessionState,
        turn: TurnRecord,
        turn_started: float,
    ) -> AsyncIterator[AgentEvent]:
        preferences, cart, facts = await self._prefetch(session)
        context = build_dynamic_context(
            preferences=preferences,
            memory_facts=facts,
            cart=cart,
            page=session.page,
            now=session.local_now(),
            max_chars=self.config.max_context_chars * 3,
        )
        system = build_system_blocks(self._static_system, context)
        executor = CartisanToolExecutor(
            backend=self.services,
            config=self.config,
            skills=self.skills,
            session=session,
            state=state,
            memory=self.memory,
        )
        forced_tool = self._forced_tool(state, latest_user_text(messages))
        usage = usage_totals()
        stop_reason: str | None = None
        last_prompt = 0
        reply_text: list[str] = []

        async def timed(name: str, arguments: dict[str, Any]) -> ToolOutcome:
            started = time.monotonic()
            outcome = await executor.execute(name, arguments)
            setattr(outcome, "_elapsed_ms", int((time.monotonic() - started) * 1000))
            return outcome

        # A turn the host abandons at a yield, or a round that raises, must not leave the
        # stored conversation on a tool_use with no result: the next request would be
        # rejected. The finally pairs any open call with its result, or an error.
        settled: dict[str, ToolOutcome] = {}
        try:
            for round_index in range(self.config.max_tool_iterations + 1):
                force_text = round_index == self.config.max_tool_iterations
                if force_text:
                    tool_choice: dict[str, str] = {"type": "none"}
                elif round_index == 0 and forced_tool:
                    tool_choice = {"type": "tool", "name": forced_tool}
                else:
                    tool_choice = {"type": "auto"}

                request: dict[str, Any] = {
                    "model": self.config.model,
                    "max_tokens": self.config.max_tokens,
                    "system": system,
                    "tools": self._tools,
                    "tool_choice": tool_choice,
                    # `tool_choice` keys the cached messages span, so the rolling marker
                    # is written only on auto rounds; an entry written under a forced
                    # round is unreadable by the auto rounds that follow.
                    "messages": build_request_messages(
                        messages,
                        rolling_breakpoint=(
                            self.config.rolling_conversation_cache
                            and tool_choice["type"] == "auto"
                        ),
                    ),
                    **self.config.thinking_request_fields(),
                }
                dispatcher = EagerDispatcher(
                    timed, self.config.eager_tool_dispatch and not force_text
                )
                streamed = StreamedRound(
                    specs=self._specs,
                    partial_tools=self._partial_ui_tools,
                    state=state,
                    eager_frames=self.config.eager_partial_frames,
                )
                call_started = time.monotonic()
                try:
                    async with self.client.messages.stream(**cast(Any, request)) as stream:
                        async for event in streamed.relay(
                            stream, dispatcher, executor.tool_call_event
                        ):
                            if event.type == "text_delta":
                                reply_text.append(event.data.get("text", ""))
                            yield event
                        final = None if streamed.abandoned else await stream.get_final_message()
                    response = final or streamed
                    log_model_call(
                        logger, request, response, call_started, session.conversation_id,
                        round=round_index,
                    )
                    if final is None:
                        reply, tool_uses, unreadable = salvage_round(
                            streamed, dispatcher, logger, session.conversation_id, round_index
                        )
                    else:
                        reply = assistant_message(final)
                        tool_uses = [block for block in final.content if block.type == "tool_use"]
                        unreadable = set()
                    stop_reason = final.stop_reason if final else "tool_use"
                    accumulate_usage(usage, response)
                    last_prompt = prompt_tokens(response)
                    if reply is not None:
                        messages.append(reply)
                    if not tool_uses or force_text:
                        break

                    for block in tool_uses:
                        if block.id in unreadable or not dispatcher.started(block.id):
                            yield executor.tool_call_event(
                                block.name, block.id, dict(block.input or {})
                            )
                    outcomes = await dispatcher.collect(tool_uses)
                finally:
                    dispatcher.cancel()

                calls = list(zip(tool_uses, outcomes, strict=True))
                settled = {block.id: outcome for block, outcome in calls}
                for block, outcome in calls:
                    self._record(turn, session, executor, block, outcome)
                    for event in outcome_events(block.name, block.id, outcome):
                        yield event
                messages.append(
                    {
                        "role": "user",
                        "content": [
                            tool_result_block(block.id, outcome) for block, outcome in calls
                        ],
                    }
                )
                settled = {}
                if self.config.close_on_presentation and round_closes_turn(
                    ((block.name, outcome) for block, outcome in calls), executor.ends_clean
                ):
                    stop_reason = "end_turn"
                    break
        finally:
            close_open_tool_uses(messages, settled)

        self.turns.complete(turn, agent_message="".join(reply_text).strip(), usage=usage)
        cleared = compact_history(
            messages,
            last_prompt,
            self.config.compact_history_above_tokens,
            session.conversation_id,
        )
        yield AgentEvent.turn_complete(stop_reason, usage, elapsed_ms(turn_started), cleared)

    # -- helpers --------------------------------------------------------------

    def _forced_tool(self, state: SessionState, user_text: str) -> str | None:
        """What the turn's first round is pinned to. Checkout outranks every grounding
        rule: a customer saying "complete my purchase" is not asking a policy question,
        whatever words they used to say it."""
        if state.checkout_turn:
            return "stage_checkout"
        return first_forced_tool(GROUNDING_RULES, self.config, user_text, state)

    def _record(
        self,
        turn: TurnRecord,
        session: SessionContext,
        executor: CartisanToolExecutor,
        block: Any,
        outcome: ToolOutcome,
    ) -> None:
        """One tool execution row and its evidence record. Every call is recorded,
        whatever it returned, and the arguments stored are the ones the tool actually
        received — the `status` line the model wrote for the person waiting is not part
        of the call and is left out."""
        arguments, _status = executor.split_status(block.name, dict(block.input or {}))
        try:
            self.turns.record_tool(
                turn,
                session,
                name=block.name,
                arguments=arguments,
                outcome=classify(outcome),
                result=outcome.result_text,
                latency_ms=int(getattr(outcome, "_elapsed_ms", 0)),
                reason=f"Agent turn {turn.sequence} called {block.name}",
                target_type=_target_type(block.name),
                target_id=_target_id(arguments),
            )
        except Exception:  # a ledger failure must not lose the customer's turn
            logger.exception("could not record tool execution for %s", block.name)

    async def _prefetch(self, session: SessionContext) -> tuple[Any, Any, list[Any]]:
        preferences, cart, facts = await asyncio.gather(
            _fetched(self.services.port.get_preferences(session)),
            _fetched(self.services.port.get_cart(session) if self.config.enable_cart else None),
            _fetched(self.memory.tier_one(session.customer_id)),
        )
        return preferences, cart, list(facts or [])

    async def update_memory(
        self, messages: list[dict[str, Any]], session: SessionContext
    ) -> list[Any]:
        """Extract what the finished turn taught and store it. Run it once the reply has
        streamed; it returns the facts written and never raises."""
        from commerce_common.turn import latest_exchange

        transcript = transcript_text(latest_exchange(messages))
        return await self.memory.extract(
            self.client, session.customer_id, session.conversation_id, transcript
        )


async def _fetched(awaitable: Any) -> Any:
    """A prefetch that must not fail the turn: a profile or cart read that raises is
    absent context, not an error the customer should see."""
    if awaitable is None:
        return None
    try:
        return await awaitable
    except Exception:
        logger.warning("prefetch failed; the turn runs without it", exc_info=True)
        return None


def _target_type(name: str) -> str | None:
    if name in {"add_to_cart", "update_cart_item", "remove_from_cart", "get_cart", "present_cart"}:
        return "cart"
    if name in {"stage_checkout", "present_checkout"}:
        return "checkout_stage"
    if name in {"get_order_status", "present_order_status"}:
        return "order"
    if name in {"search_products", "get_product_details", "check_compatibility"}:
        return "catalog_variant"
    return None


def _target_id(arguments: dict[str, Any]) -> str | None:
    for key in ("variant_id", "base_variant_id", "order_id", "stage_id", "item_ref"):
        if arguments.get(key):
            return str(arguments[key])
    return None
