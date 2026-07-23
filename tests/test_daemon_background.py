"""Background-reasoning wiring into EdithDaemon (spec 11).

Fakes only — no audio, no live model call. Asserts the daemon-specific integration:
  1. the daemon builds a BackgroundReasoner and injects it into Brain,
  2. a ``brain.background_done`` ping is spoken via VoiceIO (voiced path),
  3. ``stop()`` cancels any outstanding background job (shutdown ownership).
"""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from edith.daemon.edithd import EdithDaemon, Secrets
from edith.router import BackgroundReasoner, JobStatus, ModelResponse, Tier


class FakeRouter:
    def __init__(self) -> None:
        self.calls: list[object] = []

    async def model_call(
        self,
        messages: list[dict[str, object]],
        tier_hint: Tier,
        max_tokens: int = 1024,
    ) -> ModelResponse:
        self.calls.append(messages)
        return ModelResponse(text="ok", input_tokens=1, output_tokens=1)


class GatedRouter:
    """A router whose model_call blocks until released — to catch a job mid-flight."""

    def __init__(self) -> None:
        self.release = asyncio.Event()

    async def model_call(
        self,
        messages: list[dict[str, object]],
        tier_hint: Tier,
        max_tokens: int = 1024,
    ) -> ModelResponse:
        await self.release.wait()
        return ModelResponse(text="deep", input_tokens=1, output_tokens=1)


class SpyMemory:
    def recall(self, query: str) -> list[dict[str, object]]:
        return []

    def remember(self, nodes=None, edges=None) -> None:  # noqa: ANN001
        pass


class FakeVoiceIO:
    def __init__(self) -> None:
        self.spoken: list[str] = []

    async def speak(self, text: str) -> None:
        self.spoken.append(text)

    def set_paused(self, paused: bool) -> None:
        pass


@pytest.fixture
def data_dir() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(dir="/tmp") as d:  # noqa: S108
        yield Path(d) / "edithdata"


def _daemon(data_dir: Path, *, router=None, voice=None) -> EdithDaemon:  # noqa: ANN001
    return EdithDaemon(
        data_dir=data_dir,
        secrets=Secrets(bifrost_api_key="k", bifrost_base_url="https://x"),
        memory=SpyMemory(),
        router=router or FakeRouter(),
        voice=voice,
    )


async def test_daemon_builds_a_reasoner_and_injects_it_into_brain(data_dir: Path) -> None:
    daemon = _daemon(data_dir)
    await daemon.start()
    try:
        assert isinstance(daemon._reasoner, BackgroundReasoner)
        assert daemon._brain is not None
        assert daemon._brain._reasoner is daemon._reasoner
    finally:
        await daemon.stop()


async def test_background_done_is_spoken_via_voice(data_dir: Path) -> None:
    voice = FakeVoiceIO()
    daemon = _daemon(data_dir, voice=voice)
    await daemon.start()
    try:
        await daemon.bus.publish(
            "brain.background_done",
            source="brain",
            payload={"answer": "the sharding conclusion, sir"},
        )
    finally:
        await daemon.stop()

    assert voice.spoken == ["the sharding conclusion, sir"]


async def test_no_background_speak_subscriber_without_voice(data_dir: Path) -> None:
    # voice=None: publishing background_done must not error and nothing speaks.
    daemon = _daemon(data_dir, voice=None)
    await daemon.start()
    try:
        await daemon.bus.publish(
            "brain.background_done", source="brain", payload={"answer": "x"}
        )
    finally:
        await daemon.stop()
    # no assertion beyond "did not raise" — the subscriber must be voiced-only


async def test_stop_cancels_outstanding_background_jobs(data_dir: Path) -> None:
    router = GatedRouter()
    daemon = _daemon(data_dir, router=router)
    await daemon.start()

    async def _on_done(_r: ModelResponse) -> None:
        return None

    # Fire a job through the daemon's reasoner; it blocks on the gated router.
    assert isinstance(daemon._reasoner, BackgroundReasoner)
    job = await daemon._reasoner.think_async([{"role": "user", "content": "think"}], _on_done)
    await asyncio.sleep(0)  # let it reach the gated model_call
    assert job.status is JobStatus.RUNNING

    await daemon.stop()  # graceful shutdown must cancel it

    assert job.task is not None
    with pytest.raises(asyncio.CancelledError):
        await job.task
    assert job.status is JobStatus.CANCELLED
