"""Production health, in the same shape every other Cartisan figure has (ADR 0032).

The data was already there and only the surface was missing: `turns` stores token
and cache counts per turn, `tool_executions` stores latency and a typed outcome per
call, and the outbox and inbox record every retry and every refusal. What this
module adds is the reading — windowed, origin-aware, and each figure carrying the
formula that produced it.

Every number is a `Claim`: a value, the `basis` that computed it, the `inputs` it
was computed from, and what it cannot support. That is deliberately the *same*
shape Phase 6 settled on for merchant figures rather than a parallel one, so a
reader learns to check a number once (ADR 0017).

The trap this repeats is worth naming, because it already bit once: a figure with
no window, displayed beside windowed ones, reads as a share of them. Every claim
here states its window in `inputs`, and the ones that deliberately cover all
recorded history say so in `limitations`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from cartisan_agent.merchant_types import Claim

from .evidence import ORIGINS
from .store import Store


class HealthMetrics:
    def __init__(self, store: Store) -> None:
        self.store = store

    def _cutoff(self, hours: int) -> str:
        return (datetime.now(UTC) - timedelta(hours=max(1, hours))).isoformat()

    def report(self, *, hours: int = 24, demo_run_id: str | None = None) -> dict:
        """The whole picture for one window: the runtime, the tools, the money path,
        and the delivery machinery between them."""
        hours = max(1, min(hours, 24 * 90))
        return {
            "window_hours": hours,
            "demo_run_id": demo_run_id,
            "generated_at": datetime.now(UTC).isoformat(),
            "runtime": [claim.payload() for claim in self.runtime(hours, demo_run_id)],
            "tools": [claim.payload() for claim in self.tools(hours, demo_run_id)],
            "payments": [claim.payload() for claim in self.payments(hours)],
            "delivery": [claim.payload() for claim in self.delivery(hours)],
            "tool_outcomes": self.tool_outcomes(hours, demo_run_id),
            "origins": self.origin_counts(hours),
        }

    # -- the runtime -----------------------------------------------------------

    def runtime(self, hours: int = 24, demo_run_id: str | None = None) -> list[Claim]:
        """Turns, how they ended, and what they cost in tokens."""
        where, params = self._turn_filter(hours, demo_run_id)
        row = self.store.rows(
            "SELECT COUNT(*) AS turns, "
            "SUM(CASE WHEN state='completed' THEN 1 ELSE 0 END) AS completed, "
            "SUM(CASE WHEN state='failed' THEN 1 ELSE 0 END) AS failed, "
            "SUM(CASE WHEN state='abandoned' THEN 1 ELSE 0 END) AS abandoned, "
            "COALESCE(SUM(input_tokens),0) AS input_tokens, "
            "COALESCE(SUM(output_tokens),0) AS output_tokens, "
            "COALESCE(SUM(cache_read_tokens),0) AS cache_read_tokens "
            f"FROM turns{where}", params)[0]
        turns = int(row["turns"])
        completed, failed = int(row["completed"] or 0), int(row["failed"] or 0)
        abandoned = int(row["abandoned"] or 0)
        cached, uncached = int(row["cache_read_tokens"]), int(row["input_tokens"])
        # The API reports `input_tokens` as the tokens it actually read *fresh*, with
        # cache reads counted separately — so the whole prompt is the two added
        # together. Dividing cached by `input_tokens` alone is not a share of anything:
        # on a well-cached turn it goes far above 1 and reads as "243% cache hit rate".
        # This is the same failure `_all_time()` had in Phase 6, and again only a live
        # run showed it: every SQLite fixture has too few tokens for it to look wrong.
        prompt = cached + uncached
        window = {"window_hours": hours, "demo_run_id": demo_run_id}
        return [
            Claim(key="turns_started", value=turns, unit="turns",
                  basis="COUNT(turns) started in the window",
                  inputs={**window}),
            Claim(key="turn_completion_rate",
                  value=round(completed / turns, 4) if turns else None, unit="ratio",
                  basis="turns in state 'completed' / turns started",
                  inputs={"completed": completed, "failed": failed, "abandoned": abandoned,
                          "turns": turns, **window},
                  limitations=[] if turns else ["No turns ran in this window."]),
            Claim(key="prompt_cache_read_rate",
                  value=round(cached / prompt, 4) if prompt else None, unit="ratio",
                  basis="SUM(cache_read_tokens) / (SUM(cache_read_tokens) + SUM(input_tokens)) "
                        "over turns — cached tokens as a share of the whole prompt",
                  inputs={"cache_read_tokens": cached, "uncached_input_tokens": uncached,
                          "prompt_tokens": prompt, **window},
                  limitations=[
                      "A token share, not a request hit rate: one turn's cached prefix "
                      "counts once per model call it was read on.",
                      "Cache *writes* are not recorded on the turn, so the first call that "
                      "populates a prefix counts entirely as uncached."]
                  if prompt else ["No model calls recorded in this window."]),
            Claim(key="output_tokens", value=int(row["output_tokens"]), unit="tokens",
                  basis="SUM(output_tokens) over turns in the window", inputs={**window}),
        ]

    def tools(self, hours: int = 24, demo_run_id: str | None = None) -> list[Claim]:
        """Tool calls, how they ended, and how long they took."""
        join, params = self._tool_filter(hours, demo_run_id)
        row = self.store.rows(
            "SELECT COUNT(*) AS calls, "
            "SUM(CASE WHEN t.outcome='applied' THEN 1 ELSE 0 END) AS applied, "
            "SUM(CASE WHEN t.outcome='blocked' THEN 1 ELSE 0 END) AS blocked, "
            "SUM(CASE WHEN t.outcome='failed' THEN 1 ELSE 0 END) AS failed, "
            "COALESCE(AVG(t.latency_ms),0) AS mean_latency, "
            "COALESCE(MAX(t.latency_ms),0) AS max_latency "
            f"FROM tool_executions t{join}", params)[0]
        calls = int(row["calls"])
        blocked, failed = int(row["blocked"] or 0), int(row["failed"] or 0)
        window = {"window_hours": hours, "demo_run_id": demo_run_id}
        return [
            Claim(key="tool_calls", value=calls, unit="calls",
                  basis="COUNT(tool_executions) in the window", inputs={**window}),
            Claim(key="tool_failure_rate",
                  value=round(failed / calls, 4) if calls else None, unit="ratio",
                  basis="tool executions with outcome 'failed' / all tool executions",
                  inputs={"failed": failed, "calls": calls, **window},
                  limitations=[
                      "'blocked' is excluded from this rate: a refused call is the "
                      "system working, not a failure."] if calls
                  else ["No tool calls recorded in this window."]),
            Claim(key="policy_block_rate",
                  value=round(blocked / calls, 4) if calls else None, unit="ratio",
                  basis="tool executions with outcome 'blocked' / all tool executions",
                  inputs={"blocked": blocked, "calls": calls, **window},
                  limitations=["A block is a business refusal the model was told about, "
                               "not an error."] if calls else ["No tool calls in this window."]),
            Claim(key="tool_latency_mean_ms", value=round(float(row["mean_latency"] or 0)),
                  unit="milliseconds",
                  basis="AVG(latency_ms) over tool executions in the window",
                  inputs={"max_latency_ms": int(row["max_latency"] or 0), "calls": calls,
                          **window},
                  limitations=["A mean hides the tail; `max_latency_ms` is the worst call "
                               "in the same window."]),
        ]

    def tool_outcomes(self, hours: int = 24, demo_run_id: str | None = None) -> list[dict]:
        """Per-tool counts, so a single misbehaving tool is visible rather than
        averaged into a healthy-looking total."""
        join, params = self._tool_filter(hours, demo_run_id)
        return self.store.rows(
            "SELECT t.tool_name AS tool_name, t.outcome AS outcome, COUNT(*) AS count, "
            "COALESCE(AVG(t.latency_ms),0) AS mean_latency_ms "
            f"FROM tool_executions t{join} "
            "GROUP BY t.tool_name, t.outcome ORDER BY COUNT(*) DESC", params)

    # -- the money path --------------------------------------------------------

    def payments(self, hours: int = 24) -> list[Claim]:
        """Checkout and provider health. Windowed on the order's own creation time,
        so a ninety-day seeded history cannot inflate a one-day reading."""
        cutoff = self._cutoff(hours)
        orders = self.store.rows(
            "SELECT status, origin, COUNT(*) AS n FROM commerce_orders "
            "WHERE created_at >= ? GROUP BY status, origin", (cutoff,))
        by_status: dict[str, int] = {}
        for row in orders:
            by_status[row["status"]] = by_status.get(row["status"], 0) + int(row["n"])
        created = sum(by_status.values())
        paid = by_status.get("paid", 0)
        attempts = self.store.rows(
            "SELECT status, COUNT(*) AS n FROM payment_attempts WHERE created_at >= ? "
            "GROUP BY status", (cutoff,))
        attempt_counts = {row["status"]: int(row["n"]) for row in attempts}
        total_attempts = sum(attempt_counts.values())
        quarantined = int(self.store.rows(
            "SELECT COUNT(*) AS n FROM inbox_events WHERE status='quarantined' "
            "AND received_at >= ?", (cutoff,))[0]["n"])
        events = int(self.store.rows(
            "SELECT COUNT(*) AS n FROM inbox_events WHERE received_at >= ?", (cutoff,))[0]["n"])
        window = {"window_hours": hours}
        return [
            Claim(key="orders_created", value=created, unit="orders",
                  basis="COUNT(commerce_orders) created in the window",
                  inputs={"by_status": by_status, "by_origin": {
                      origin: sum(int(r["n"]) for r in orders if r["origin"] == origin)
                      for origin in ORIGINS}, **window}),
            Claim(key="verified_payment_rate",
                  value=round(paid / created, 4) if created else None, unit="ratio",
                  basis="orders in status 'paid' / orders created, both within the window",
                  inputs={"paid": paid, "created": created, **window},
                  limitations=[
                      "An order created near the end of the window may still be in flight, "
                      "so a short window understates this.",
                      "'paid' here means a verified provider event was applied, never a "
                      "browser redirect."] if created else ["No orders created in this window."]),
            Claim(key="payment_attempts", value=total_attempts, unit="attempts",
                  basis="COUNT(payment_attempts) created in the window",
                  inputs={"by_status": attempt_counts, **window},
                  limitations=["Several attempts can belong to one order; a retry is an "
                               "attempt, never a second order."]),
            Claim(key="provider_event_quarantine_rate",
                  value=round(quarantined / events, 4) if events else None, unit="ratio",
                  basis="inbox events quarantined / inbox events received, within the window",
                  inputs={"quarantined": quarantined, "events": events, **window},
                  limitations=["A quarantine is verification refusing a payload; it is "
                               "evidence of the check working."]
                  if events else ["No provider events received in this window."]),
        ]

    def delivery(self, hours: int = 24) -> list[Claim]:
        """The outbox: what is waiting, what was retried, and what is parked.

        `dead_letters` is deliberately not windowed — a message parked last week is
        still parked today, and windowing it would hide the only figure here that
        needs a human. It says so in its own limitations rather than sitting
        unlabelled beside the windowed ones.
        """
        cutoff = self._cutoff(hours)
        rows = self.store.rows(
            "SELECT status, COUNT(*) AS n FROM outbox_messages WHERE created_at >= ? "
            "GROUP BY status", (cutoff,))
        counts = {row["status"]: int(row["n"]) for row in rows}
        total = sum(counts.values())
        retries = int(self.store.rows(
            "SELECT COALESCE(SUM(attempts),0) AS n FROM outbox_messages "
            "WHERE created_at >= ? AND attempts > 1", (cutoff,))[0]["n"])
        parked = int(self.store.rows(
            "SELECT COUNT(*) AS n FROM outbox_messages WHERE status='dead_letter'")[0]["n"])
        return [
            Claim(key="outbox_messages", value=total, unit="messages",
                  basis="COUNT(outbox_messages) created in the window",
                  inputs={"by_status": counts, "window_hours": hours}),
            Claim(key="outbox_delivery_rate",
                  value=round(counts.get("delivered", 0) / total, 4) if total else None,
                  unit="ratio",
                  basis="messages in status 'delivered' / messages created, within the window",
                  inputs={"delivered": counts.get("delivered", 0), "messages": total,
                          "window_hours": hours},
                  limitations=["A message created near the end of the window may not have "
                               "been drained yet."] if total
                  else ["No outbox messages created in this window."]),
            Claim(key="outbox_retry_attempts", value=retries, unit="attempts",
                  basis="SUM(attempts) over messages in the window that took more than one",
                  inputs={"window_hours": hours}),
            Claim(key="dead_letters", value=parked, unit="messages",
                  basis="COUNT(outbox_messages) in status 'dead_letter', all recorded history",
                  inputs={"window_hours": None},
                  limitations=[
                      "Not windowed, unlike every other figure here: a message parked "
                      "before this window is still parked now and still needs a human.",
                      "Each one is a recoverable external effect; see the recovery queue."]),
        ]

    def origin_counts(self, hours: int = 24) -> list[dict]:
        """Evidence by origin, so a reader never has to guess which kind of record a
        figure came from (ADR 0008, ADR 0032)."""
        return self.store.rows(
            "SELECT data_origin, COUNT(*) AS count FROM evidence_records "
            "WHERE recorded_at >= ? GROUP BY data_origin ORDER BY COUNT(*) DESC",
            (self._cutoff(hours),))

    # -- filters ---------------------------------------------------------------

    def _turn_filter(self, hours: int, demo_run_id: str | None) -> tuple[str, tuple[Any, ...]]:
        if demo_run_id:
            return " WHERE started_at >= ? AND demo_run_id = ?", (self._cutoff(hours), demo_run_id)
        return " WHERE started_at >= ?", (self._cutoff(hours),)

    def _tool_filter(self, hours: int, demo_run_id: str | None) -> tuple[str, tuple[Any, ...]]:
        """Tool executions carry no demo run of their own; the turn they belong to
        does, so narrowing by demo run joins rather than guessing."""
        if demo_run_id:
            return (" JOIN turns ON turns.id = t.turn_id WHERE t.created_at >= ? "
                    "AND turns.demo_run_id = ?", (self._cutoff(hours), demo_run_id))
        return " WHERE t.created_at >= ?", (self._cutoff(hours),)


__all__ = ["HealthMetrics"]
