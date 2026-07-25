"""Background / two-agent reasoning over the Router (specs 12 + 13).

The north-star "two agents, fast masks slow" philosophy, made concrete as SEPARATE model
calls — never one inference, never shared weights.

- ``supervised_reason`` (SYNCHRONOUS, awaited, spec 12): a fast draft then a strong review
  pass that critiques+improves it, returning the REFINED response. Consumer: Brain's
  deep-query path.
- ``BackgroundReasoner.think_async`` (BACKGROUND, spec 13): **opus never blocks the live
  turn.** When Brain judges a turn deep enough for opus, it does NOT await opus inline
  (2–5 s of dead air); it fires a tracked background job here and speaks/acks on Sonnet
  immediately. The job runs opus off the critical path and calls ``on_done`` when it lands,
  so Brain can summarize + ``remember`` + ping the owner.

  This **supersedes spec 12's free-function ``think_async``**, which was a seam with no
  production consumer and no tracking. It generalizes the fire-and-forget pattern in
  ``finder/resolve.py`` (``_deep_extract``) into a first-class, **tracked** mechanism:
  unlike an untracked ``create_task``, a ``BackgroundJob`` exposes ``.status`` /
  ``.cancel()``, and its task is held in a registry (a detached task with no live reference
  can be GC'd mid-flight). The daemon owns shutdown via ``cancel_all()``.

Placement mirrors ``model_call_masked`` (spec 05 §Division of responsibility): Brain decides
WHEN and supplies ``on_done``; the reasoner provides the mechanism (budget-gate → tracked
opus task → notify). Budget is gated BEFORE the job starts — a denied opus job never runs
(it does not silently downgrade to a pointless background Sonnet re-run, since Sonnet
already answered the live turn).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from itertools import count
from typing import Protocol

from edith.router.bifrost import (
    _DEFAULT_MAX_TOKENS,
    MODEL_CALL_ERRORS,
    BudgetCheck,
    ModelResponse,
)
from edith.router.tiers import Tier


class RouterLike(Protocol):
    """The slice of the Router both entry points need (avoids a hard Router dependency).

    Structural, so a fake router in tests and the real ``Router`` satisfy it identically —
    that is the point: no ``type: ignore`` at the call sites.
    """

    async def model_call(
        self,
        messages: list[dict[str, object]],
        tier_hint: Tier,
        max_tokens: int = ...,
    ) -> ModelResponse: ...


OnDone = Callable[[ModelResponse], Awaitable[None]]

# Router-owned instruction for the review pass. The reviewer gets the full original context
# plus the draft (folded in as the assistant turn) and is asked to critique+improve it —
# it refines the draft, it does not re-answer blind.
_REVIEW_INSTRUCTION = (
    "Critique the draft answer above and produce an improved, final version. "
    "Fix any errors, fill gaps, and tighten it. Return only the improved answer."
)


async def supervised_reason(
    router: RouterLike,
    messages: list[dict[str, object]],
    *,
    draft_tier: Tier = Tier.SONNET,
    review_tier: Tier = Tier.OPUS,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
) -> ModelResponse:
    """Draft-then-review: a fast draft, then a strong critique+improve pass.

    Two separate ``model_call``s (fast masks slow, spec 05). The draft is folded into the
    review payload as an assistant turn so the reviewer refines it rather than re-answering
    blind. Returns the REFINED response (the second call).
    """
    draft = await router.model_call(messages, draft_tier, max_tokens)
    review_messages: list[dict[str, object]] = [
        *messages,
        {"role": "assistant", "content": draft.text},
        {"role": "user", "content": _REVIEW_INSTRUCTION},
    ]
    return await router.model_call(review_messages, review_tier, max_tokens)


class JobStatus(Enum):
    """Lifecycle of a background reasoning job."""

    RUNNING = "running"      # opus is in flight
    DONE = "done"            # opus completed; on_done fired
    FAILED = "failed"        # opus raised a declared transport error; on_done skipped
    DENIED = "denied"        # budget denied opus → never started
    CANCELLED = "cancelled"  # cancelled (per-job or daemon shutdown)


@dataclass
class BackgroundJob:
    """Handle to a background reasoning job (spec 13 §Interface).

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
    """Fires and tracks background opus jobs (spec 13)."""

    def __init__(
        self,
        router: RouterLike,
        *,
        budget_check: BudgetCheck = lambda _tier: True,
    ) -> None:
        self._router = router
        # Guard seam: opus is the expensive tier → gate before starting. `edith/guard/guard.py`
        # (merged, PR #17) is the intended injection here once the composition root constructs
        # a Guard; the default stays permissive so nothing changes until it does.
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
