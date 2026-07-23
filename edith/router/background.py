"""Background reasoning — ``think_async`` (spec 11).

The routing philosophy's centerpiece: **opus never blocks the live turn.** When Brain judges a
turn deep enough for opus, it does NOT await opus inline (2–5 s of dead air); it fires a
background job here and speaks/acks on Sonnet immediately. The job runs opus off the critical
path and calls ``on_done`` when it lands, so Brain can summarize + ``remember`` + ping the owner.

This generalizes the fire-and-forget pattern in ``finder/resolve.py`` (``_deep_extract``) into a
first-class, **tracked** mechanism: unlike that untracked ``create_task``, a ``BackgroundJob``
exposes ``.status`` / ``.cancel()``, so its task is held in a registry (a detached task with no
live reference can be GC'd mid-flight). The daemon owns shutdown via ``cancel_all()``.

Placement mirrors ``model_call_masked`` (spec 05 §Division of responsibility): Brain decides WHEN
and supplies ``on_done``; the reasoner provides the mechanism (budget-gate → tracked opus task →
notify). Budget is gated BEFORE the job starts — a denied opus job never runs (it does not silently
downgrade to a pointless background Sonnet re-run, since Sonnet already answered the live turn).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from itertools import count
from typing import Protocol

from edith.router.bifrost import MODEL_CALL_ERRORS, BudgetCheck, ModelResponse
from edith.router.tiers import Tier

# Background opus can be long; give it more room than a live spoken reply.
_DEFAULT_MAX_TOKENS = 1024


class RouterLike(Protocol):
    """The slice of the Router the reasoner needs (avoids a hard Router dependency)."""

    async def model_call(
        self,
        messages: list[dict[str, object]],
        tier_hint: Tier,
        max_tokens: int = ...,
    ) -> ModelResponse: ...


OnDone = Callable[[ModelResponse], Awaitable[None]]


class JobStatus(Enum):
    """Lifecycle of a background reasoning job."""

    RUNNING = "running"      # opus is in flight
    DONE = "done"            # opus completed; on_done fired
    FAILED = "failed"        # opus raised a declared transport error; on_done skipped
    DENIED = "denied"        # budget denied opus → never started
    CANCELLED = "cancelled"  # cancelled (per-job or daemon shutdown)


@dataclass
class BackgroundJob:
    """Handle to a background reasoning job (spec 11 §Interface).

    ``task`` is the underlying opus task (``None`` for a DENIED job that never started). It is a
    public field because ``cancel()``, the reasoner's shutdown sweep, and tests all legitimately
    need it — not a test-only hook.
    """

    id: str
    status: JobStatus
    task: asyncio.Task[None] | None = field(default=None)

    def cancel(self) -> None:
        """Cancel the underlying opus task (no-op if it never started or already finished)."""
        if self.task is not None:
            self.task.cancel()


class BackgroundReasoner:
    """Fires and tracks background opus jobs (spec 11)."""

    def __init__(
        self,
        router: RouterLike,
        *,
        budget_check: BudgetCheck = lambda _tier: True,
    ) -> None:
        self._router = router
        # Guard seam (deferred slice): opus is the expensive tier → gate before starting.
        self._budget_check = budget_check
        self._tasks: set[asyncio.Task[None]] = set()
        self._ids = count(1)

    async def think_async(
        self,
        messages: list[dict[str, object]],
        on_done: OnDone,
        *,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
    ) -> BackgroundJob:
        """Fire a background opus job, returning a handle IMMEDIATELY (non-blocking).

        Budget-denied opus never starts (status DENIED). Otherwise opus runs as a tracked task;
        on success ``on_done`` fires with the result and status → DONE.
        """
        job_id = f"job-{next(self._ids)}"
        if not self._budget_check(Tier.OPUS):
            return BackgroundJob(id=job_id, status=JobStatus.DENIED, task=None)

        job = BackgroundJob(id=job_id, status=JobStatus.RUNNING, task=None)
        task = asyncio.create_task(self._run(job, messages, on_done, max_tokens))
        job.task = task
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job

    def cancel_all(self) -> None:
        """Cancel every outstanding job (daemon shutdown; mirrors ``_session_tasks``)."""
        for task in list(self._tasks):
            task.cancel()

    async def _run(
        self,
        job: BackgroundJob,
        messages: list[dict[str, object]],
        on_done: OnDone,
        max_tokens: int,
    ) -> None:
        try:
            result = await self._router.model_call(messages, Tier.OPUS, max_tokens)
        except asyncio.CancelledError:
            job.status = JobStatus.CANCELLED
            raise
        except MODEL_CALL_ERRORS:
            # A detached task's unhandled exception vanishes silently and on_done never
            # fires; catch the router's declared failure tuple (specific, never bare) so the
            # status reflects it. CancelledError is not in this tuple → cancellation propagates.
            job.status = JobStatus.FAILED
            return
        job.status = JobStatus.DONE
        await on_done(result)
