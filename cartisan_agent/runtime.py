"""The turn loop both Cartisan surfaces run on (ADR 0014).

One model call per round, tools dispatched as their content blocks close, a
rolling cache breakpoint through the conversation, and the turn ending on a round
of clean presentation calls. Adapted from the checked-in reference runtime; what
is Cartisan's is what surrounds the loop — the deterministic route decided ahead
of the first round, the turn and tool-execution rows every call writes, and the
typed outcomes those rows carry.

The loop itself is identical on both surfaces, and deliberately lives in one
place: a fix to how a turn recovers from an abandoned round, or how usage is
accumulated, has to reach shopping and merchant together or the two drift into
different runtimes wearing the same name. A subclass supplies what actually
differs — its tools, its prompt halves, its executor, what its first round is
pinned to, and how a tool call names its target — and nothing else.
"""

from __future__ import annotations

import json
import logging
import os
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any, cast

from anthropic import AsyncAnthropic

from commerce_common.execution import BaseToolExecutor
from commerce_common.memory import MemoryRuntime
from commerce_common.presentation import PresentationComponent, partial_ui_tool_names
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
from .outcomes import classify
from .turns import TurnInProgress, TurnRecord, TurnStore, conversation_lock

logger = logging.getLogger(__name__)


class AgentRuntime(ABC):
    """One deployment's turn loop. Built once per process: the static prompt and the
    tool array are the same bytes on every request, which is what makes them the
    cacheable prefix (ADR 0028)."""

    prompt_version: str

    def __init__(
        self,
        *,
        store: Store,
        config: CartisanAgentConfig,
        skills: SkillRegistry,
        components: dict[str, PresentationComponent],
        tools: list[dict[str, Any]],
        static_system: str,
        memory: MemoryRuntime,
        client: AsyncAnthropic | None = None,
        turns: TurnStore | None = None,
    ) -> None:
        self.config = config
        self.skills = skills
        # An identity-linked API key must name the workspace it acts in, or every
        # request is rejected with a 400 before the turn starts.
        workspace_id = os.getenv("ANTHROPIC_WORKSPACE_ID")
        self.client = client or AsyncAnthropic(
            timeout=self.config.request_timeout_s,
            default_headers={"anthropic-workspace-id": workspace_id} if workspace_id else None,
        )
        self.memory = memory
        self.turns = turns or TurnStore(store)
        self._static_system = static_system
        self._specs = components
        self._partial_ui_tools = partial_ui_tool_names(components, ())
        self._tools = with_tool_cache_control(with_eager_input(tools, self._partial_ui_tools))
        from .prompts import prompt_fingerprint

        self.prompt_fingerprint = prompt_fingerprint(self._static_system, self._tools)
        self.skill_versions = json.dumps(
            [
                f"{name}@{_digest(self.skills.get_instructions(name) or '')}"
                for name in self.skills.names
            ]
        )

    # -- what a surface supplies -------------------------------------------------

    @abstractmethod
    def build_executor(self, session: Any, state: Any) -> BaseToolExecutor:
        """The executor for one turn, holding this turn's session and provenance."""

    @abstractmethod
    async def dynamic_context(self, session: Any) -> str:
        """The per-request prompt block, behind the cache breakpoint."""

    @abstractmethod
    def forced_tool(self, state: Any, user_text: str) -> str | None:
        """What this turn's first round is pinned to, or None."""

    def before_turn(self, state: Any, user_text: str) -> None:
        """Deterministic routing decided in code, before the model is asked anything."""

    def new_state(self) -> Any:
        """A fresh conversation state, when the host did not carry one."""
        raise NotImplementedError

    def target_type(self, name: str) -> str | None:
        return None

    def target_id(self, arguments: dict[str, Any]) -> str | None:
        return None

    # -- one turn -----------------------------------------------------------------

    async def stream_turn(
        self,
        messages: list[dict[str, Any]],
        session: Any,
        state: Any = None,
    ) -> AsyncIterator[AgentEvent]:
        state = state if state is not None else self.new_state()
        turn_started = time.monotonic()
        user_text = latest_user_text(messages)
        self.before_turn(state, user_text)

        async with conversation_lock(session.conversation_id):
            try:
                turn = self.turns.begin(
                    session,
                    user_message=user_text,
                    prompt_version=f"{self.prompt_version}+{self.prompt_fingerprint}",
                    tool_contract_version=_contract_version(),
                    skill_versions=self.skill_versions,
                    skill_names=self.skills.names,
                )
                # Everything the turn touches reads its lineage off the session: the
                # ports, the executor and the repositories under them all take their
                # correlation from here rather than minting one each (ADR 0032).
                session.correlation_id = turn.correlation_id
                session.turn_id = turn.turn_id
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
        session: Any,
        state: Any,
        turn: TurnRecord,
        turn_started: float,
    ) -> AsyncIterator[AgentEvent]:
        system = build_system_blocks(self._static_system, await self.dynamic_context(session))
        executor = self.build_executor(session, state)
        forced_tool = self.forced_tool(state, latest_user_text(messages))
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

    # -- evidence -----------------------------------------------------------------

    def _record(
        self,
        turn: TurnRecord,
        session: Any,
        executor: BaseToolExecutor,
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
                target_type=self.target_type(block.name),
                target_id=self.target_id(arguments),
            )
        except Exception:  # a ledger failure must not lose the turn
            logger.exception("could not record tool execution for %s", block.name)

    async def update_memory(self, messages: list[dict[str, Any]], session: Any) -> list[Any]:
        """Extract what the finished turn taught and store it. Run it once the reply has
        streamed; it returns the facts written and never raises."""
        from commerce_common.turn import latest_exchange

        transcript = transcript_text(latest_exchange(messages))
        return await self.memory.extract(
            self.client, session.user_id, session.conversation_id, transcript
        )


async def fetched(awaitable: Any) -> Any:
    """A prefetch that must not fail the turn: a profile, cart, or snapshot read that
    raises is absent context, not an error the person should see."""
    if awaitable is None:
        return None
    try:
        return await awaitable
    except Exception:
        logger.warning("prefetch failed; the turn runs without it", exc_info=True)
        return None


def _digest(body: str) -> str:
    from .versions import digest

    return digest([body])


def _contract_version() -> str:
    from .versions import TOOL_CONTRACT_VERSION

    return TOOL_CONTRACT_VERSION
