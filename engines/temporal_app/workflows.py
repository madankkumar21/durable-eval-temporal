"""The workflow: iterate items, retry per item, wait for a human, publish.

Workflow code must be deterministic, so it only sequences activities. All I/O,
numpy and the Anthropic SDK live in activities.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from .activities import answer_item_activity, progress_activity, publish_activity, summarize_activity
    from . import shared
    from .shared import EvalParams, ItemCall, Progress, RunSummary

# Scenario 1: rate limits. Exponential backoff, capped; fatal errors never retry.
MODEL_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=60),
    maximum_attempts=8,
    non_retryable_error_types=["Fatal"],
)


@workflow.defn
class EvalWorkflow:
    def __init__(self) -> None:
        self.params: EvalParams | None = None
        self.decision: str | None = None
        self.progress: int = 0
        self.summary: RunSummary | None = None

    @workflow.signal
    def approve(self, decision: str) -> None:
        """Scenario 3: a person decides whether the results publish. Kept for approving while no
        worker runs: a signal is stored by the server, but nothing checks it."""
        self.decision = decision

    @workflow.update
    def decide(self, decision: str) -> str:
        """Scenario 3: the same decision, but the sender gets an answer or a refusal."""
        self.decision = decision
        return f"accepted: {decision}"

    @decide.validator
    def validate_decide(self, decision: str) -> None:
        # Runs on the worker before the update is accepted; a refusal is not written to history.
        # Plain state checks only, so it can be unit-tested without a server.
        if decision not in ("yes", "no"):
            raise ValueError(f"decision must be yes or no, not {decision!r}")
        if self.decision is not None:
            raise ValueError(f"already decided: {self.decision}")
        if self.summary is None:
            n = self.params.n if self.params else "?"
            raise ValueError(f"still scoring: {self.progress} of {n} items")

    @workflow.query
    def status(self) -> dict:
        return {
            "progress": self.progress,
            "n": self.params.n if self.params else None,
            "waiting_for_approval": self.summary is not None and self.decision is None,
            "decision": self.decision,
            "summary": self.summary.__dict__ if self.summary else None,
        }

    async def _answer(self, params: EvalParams, i: int) -> dict:
        return await workflow.execute_activity(
            answer_item_activity,
            ItemCall(params.run_id, params.model_id, params.task, i, params.n_frames,
                     params.shuffle_seed, params.label_style),
            start_to_close_timeout=timedelta(seconds=180),
            # Scenario 7: the activity heartbeats once it starts. A call silent for longer than
            # this is timed out and retried under MODEL_RETRY.
            heartbeat_timeout=timedelta(seconds=params.heartbeat_s),
            retry_policy=MODEL_RETRY,
        )

    async def _report(self, params: EvalParams, done: int) -> None:
        await workflow.execute_activity(
            progress_activity, Progress(params.run_id, done, params.n),
            start_to_close_timeout=timedelta(seconds=10),
        )

    @workflow.run
    async def run(self, params: EvalParams) -> dict:
        self.params = params
        rows = list(params.rows)
        self.progress = params.start_index

        i = params.start_index
        while i < params.n:
            # Scenario 6: up to `concurrency` model calls in flight. With 1 this is the plain sequence.
            batch = range(i, min(i + params.concurrency, params.n))
            # Scenario 2: each completed activity is journaled in the workflow history.
            # If the worker dies, replay skips finished items and resumes here.
            rows += await asyncio.gather(*(self._answer(params, j) for j in batch))
            i = batch[-1] + 1
            self.progress = i

            # Scenario 4: a step added after runs were already in flight. patched() is False while
            # replaying history written before the change, so old runs keep their old path.
            if shared.WORKFLOW_VERSION == "v2" and workflow.patched("progress-v2"):
                await self._report(params, i)
            elif shared.WORKFLOW_VERSION == "v2-unpatched":
                await self._report(params, i)

            # Scenario 5: history has a hard limit (51,200 events). Hand the state to a
            # fresh run before we get there.
            if i < params.n and (i - params.start_index >= params.chunk
                                 or workflow.info().is_continue_as_new_suggested()):
                workflow.continue_as_new(
                    EvalParams(**{**params.__dict__, "start_index": i, "rows": rows})
                )

        self.summary = await workflow.execute_activity(
            summarize_activity, rows, start_to_close_timeout=timedelta(seconds=60)
        )

        # Scenario 3: block until a person decides. The run survives worker restarts while waiting.
        # Scenario 3b: approval_timeout_s adds a durable timer. It fires on the server, so the wait
        # ends on time even with no worker running. None starts no timer, so old histories still replay.
        timeout = timedelta(seconds=params.approval_timeout_s) if params.approval_timeout_s else None
        try:
            await workflow.wait_condition(lambda: self.decision is not None, timeout=timeout)
        except asyncio.TimeoutError:
            self.decision = "expired"
        if self.decision != "yes":
            return {"published": False, "decision": self.decision, "summary": self.summary.__dict__}

        path = await workflow.execute_activity(
            publish_activity, args=[params.run_id, rows], start_to_close_timeout=timedelta(seconds=60)
        )
        return {"published": True, "path": path, "summary": self.summary.__dict__}
