"""Background reasoning — think_async (spec 11).

The tracked, budget-gated, cancellable background-opus mechanism. No live Bifrost: a fake
router whose ``model_call`` is gated on an ``asyncio.Event`` so the tests can prove the job is
RUNNING *before* opus completes (non-blocking is the whole feature — completion-ordering alone
would pass even for a blocking impl).
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from edith.router import BackgroundJob, BackgroundReasoner, JobStatus, ModelResponse, Tier


class FakeOpusRouter:
    """A router whose model_call blocks on ``release`` so completion is test-controlled."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[dict[str, object]], Tier, int]] = []
        self.release = asyncio.Event()
        self.response = ModelResponse(text="deep answer", input_tokens=10, output_tokens=20)
        self.raise_exc: Exception | None = None

    async def model_call(
        self,
        messages: list[dict[str, object]],
        tier_hint: Tier,
        max_tokens: int = 1024,
    ) -> ModelResponse:
        self.calls.append((messages, tier_hint, max_tokens))
        await self.release.wait()
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.response


async def test_think_async_returns_running_before_opus_completes() -> None:
    router = FakeOpusRouter()
    reasoner = BackgroundReasoner(router)
    done: list[ModelResponse] = []

    async def on_done(r: ModelResponse) -> None:
        done.append(r)

    job = await reasoner.think_async([{"role": "user", "content": "think about X"}], on_done)

    assert isinstance(job, BackgroundJob)
    assert job.status is JobStatus.RUNNING
    await asyncio.sleep(0)  # let the task issue the opus call (still gated on release)
    assert len(router.calls) == 1
    assert router.calls[0][1] is Tier.OPUS  # background work runs on opus
    assert done == []  # on_done has NOT fired — opus is still in flight


async def test_on_done_fires_with_result_and_status_done() -> None:
    router = FakeOpusRouter()
    reasoner = BackgroundReasoner(router)
    done: list[ModelResponse] = []

    async def on_done(r: ModelResponse) -> None:
        done.append(r)

    job = await reasoner.think_async([{"role": "user", "content": "think"}], on_done)
    router.release.set()
    assert job.task is not None
    await job.task  # deterministically await completion

    assert job.status is JobStatus.DONE
    assert done == [router.response]


async def test_budget_deny_never_starts_the_job() -> None:
    router = FakeOpusRouter()
    reasoner = BackgroundReasoner(router, budget_check=lambda _t: False)
    done: list[ModelResponse] = []

    async def on_done(r: ModelResponse) -> None:
        done.append(r)

    job = await reasoner.think_async([{"role": "user", "content": "think"}], on_done)

    assert job.status is JobStatus.DENIED
    assert job.task is None
    await asyncio.sleep(0)
    assert router.calls == []  # opus was never called
    assert done == []


async def test_cancel_stops_the_task_and_on_done_never_fires() -> None:
    router = FakeOpusRouter()
    reasoner = BackgroundReasoner(router)
    done: list[ModelResponse] = []

    async def on_done(r: ModelResponse) -> None:
        done.append(r)

    job = await reasoner.think_async([{"role": "user", "content": "think"}], on_done)
    await asyncio.sleep(0)  # task is now awaiting release
    job.cancel()
    assert job.task is not None
    with pytest.raises(asyncio.CancelledError):
        await job.task

    assert job.status is JobStatus.CANCELLED
    assert done == []


async def test_transport_failure_sets_failed_and_skips_on_done() -> None:
    router = FakeOpusRouter()
    router.raise_exc = httpx.ConnectError("bifrost unreachable")  # a MODEL_CALL_ERRORS member
    reasoner = BackgroundReasoner(router)
    done: list[ModelResponse] = []

    async def on_done(r: ModelResponse) -> None:
        done.append(r)

    job = await reasoner.think_async([{"role": "user", "content": "think"}], on_done)
    router.release.set()
    assert job.task is not None
    await job.task

    assert job.status is JobStatus.FAILED
    assert done == []  # a failed job must not notify with a bogus result


async def test_cancel_all_cancels_every_outstanding_job() -> None:
    router = FakeOpusRouter()
    reasoner = BackgroundReasoner(router)

    async def on_done(_r: ModelResponse) -> None:
        return None

    job1 = await reasoner.think_async([{"role": "user", "content": "a"}], on_done)
    job2 = await reasoner.think_async([{"role": "user", "content": "b"}], on_done)
    await asyncio.sleep(0)

    reasoner.cancel_all()

    for job in (job1, job2):
        assert job.task is not None
        with pytest.raises(asyncio.CancelledError):
            await job.task
        assert job.status is JobStatus.CANCELLED


async def test_job_ids_are_unique() -> None:
    router = FakeOpusRouter()
    reasoner = BackgroundReasoner(router, budget_check=lambda _t: False)

    async def on_done(_r: ModelResponse) -> None:
        return None

    ids = {(await reasoner.think_async([], on_done)).id for _ in range(5)}
    assert len(ids) == 5
