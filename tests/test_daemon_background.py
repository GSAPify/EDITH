"""Background-reasoning wiring into EdithDaemon (spec 13).

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


def test_session_narration_can_be_disabled_from_the_cli() -> None:
    """--no-session-narration must reach EdithDaemon, or wake stays deaf.

    Narration blinds the wake word: _gate_action returns "skip" for every frame while
    is_speaking, plus a 2.5s cooldown after. With several active Claude Code sessions the
    Narrator can speak near-continuously and "Hey Edith" is never detected. The toggle was
    hardcoded True with no way to turn it off — same class as --graph-refresh being
    unreachable.
    """
    import edith.daemon.__main__ as dmain

    seen: dict[str, object] = {}

    async def fake_amain(
        engine, data_dir, graph_refresh=False, session_narration=True, show_transcript=False
    ):
        seen.update(
            engine=engine,
            graph_refresh=graph_refresh,
            session_narration=session_narration,
            show_transcript=show_transcript,
        )
        return 0

    original = dmain._amain
    dmain._amain = fake_amain
    try:
        assert dmain.main(["--no-session-narration"]) == 0
        assert seen["session_narration"] is False
        seen.clear()
        assert dmain.main([]) == 0
        assert seen["session_narration"] is True  # default unchanged
    finally:
        dmain._amain = original


def test_show_transcript_flag_reaches_amain_and_defaults_off() -> None:
    """--show-transcript is the only way to see what EDITH heard in full daemon mode.

    The transcript echo lives in edith.voice.__main__ (voice-only), not the daemon, so
    running the full daemon gave no way to tell "she never heard me" apart from "she heard
    me and could not reply". Default must stay OFF: under launchd stdout is the unrotated,
    unredacted edithd.out.log.
    """
    import edith.daemon.__main__ as dmain

    seen: dict[str, object] = {}

    async def fake_amain(engine, data_dir, graph_refresh=False, session_narration=True,
                         show_transcript=False):
        seen.update(show_transcript=show_transcript)
        return 0

    original = dmain._amain
    dmain._amain = fake_amain
    try:
        assert dmain.main(["--show-transcript"]) == 0
        assert seen["show_transcript"] is True
        seen.clear()
        assert dmain.main([]) == 0
        assert seen["show_transcript"] is False  # never on by default
    finally:
        dmain._amain = original


async def test_transcript_echo_prints_heard_and_spoken(capsys) -> None:
    """The echo must fire on the real bus topics — heard input AND spoken answer."""
    from edith.bus import EventBus
    from edith.daemon.__main__ import _wire_transcript_echo

    bus = EventBus()
    _wire_transcript_echo(bus)
    await bus.publish("voice.utterance", "voice_io", {"text": "what is up", "confidence": 0.9})
    await bus.publish("brain.decision", "brain", {"answer": "All nominal, sir."})

    out = capsys.readouterr().out
    assert "[heard] 'what is up'" in out
    assert "[edith] 'All nominal, sir.'" in out


def test_daemon_guard_is_allowlisted_to_desktop_intents() -> None:
    """The composition root's Guard must deny anything outside the desktop vocabulary.

    Guard's denylist alone is disjoint from DesktopControlSkill's four Intents, so a
    bare Guard() returns ALLOW for every OS action the daemon can take (spec 11 gap).
    _build_guard's allowlist is what actually closes it.
    """
    import edith.daemon.__main__ as dmain
    from edith.desktop import Intent
    from edith.guard import Decision

    guard = dmain._build_guard()
    for intent in Intent:
        assert guard.authorize(intent.value) is Decision.ALLOW
    assert guard.authorize("some_future_intent_nobody_vetted") is Decision.DENY
