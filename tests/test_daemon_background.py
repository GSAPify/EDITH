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


async def test_daemon_voice_loop_honors_edith_wake_model(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The daemon must resolve EDITH_WAKE_MODEL, not take run_live_loop's default.

    _start_voice_loop called run_live_loop(voice) bare, so it took the wake_model
    default of "hey_jarvis" and EDITH_WAKE_MODEL was silently ignored: the daemon
    listened for "Hey Jarvis" while the owner said "Hey Edith", the trained
    hey_edith.onnx was never loaded, and wake scored ~0.00 with no error logged
    anywhere. The voice-only entry point (edith.voice.__main__) resolved it
    correctly, which is why voice-only worked and the full daemon did not.
    """
    import edith.voice.live as live

    seen: dict[str, object] = {}

    async def fake_run_live_loop(voice_io, **kwargs):  # noqa: ANN001, ANN003
        seen.update(kwargs)

    monkeypatch.setattr(live, "run_live_loop", fake_run_live_loop)
    monkeypatch.setenv("EDITH_WAKE_MODEL", "/models/hey_edith.onnx")
    monkeypatch.setenv("EDITH_WAKE_THRESHOLD", "0.42")

    daemon = _daemon(data_dir, voice=FakeVoiceIO())
    daemon._start_voice_loop(FakeVoiceIO())
    assert daemon._voice_task is not None
    await daemon._voice_task

    assert seen["wake_model"] == "/models/hey_edith.onnx"  # not "hey_jarvis"
    assert seen["wake_threshold"] == 0.42


async def test_daemon_voice_loop_falls_back_to_bundled_default(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no EDITH_WAKE_MODEL, the bundled hey_jarvis default still applies."""
    import edith.voice.live as live

    seen: dict[str, object] = {}

    async def fake_run_live_loop(voice_io, **kwargs):  # noqa: ANN001, ANN003
        seen.update(kwargs)

    monkeypatch.setattr(live, "run_live_loop", fake_run_live_loop)
    monkeypatch.delenv("EDITH_WAKE_MODEL", raising=False)
    monkeypatch.delenv("EDITH_WAKE_THRESHOLD", raising=False)

    daemon = _daemon(data_dir, voice=FakeVoiceIO())
    daemon._start_voice_loop(FakeVoiceIO())
    assert daemon._voice_task is not None
    await daemon._voice_task

    assert seen["wake_model"] == "hey_jarvis"
    assert seen["wake_threshold"] == 0.5


async def test_stop_sets_the_voice_stop_flag_and_joins_the_thread(data_dir: Path) -> None:
    """The mic loop must be stopped cooperatively, not just cancelled.

    asyncio.to_thread cannot interrupt a worker and cancelling the task around it does
    not stop the thread, so the mic thread used to outlive shutdown holding an open
    PortAudio stream — the interpreter then tore down underneath it and Ctrl-C ended in
    a segfault. stop() must set the flag and await the loop's own exit.
    """
    import threading

    import edith.voice.live as live

    started = threading.Event()
    observed_stop: dict[str, threading.Event] = {}

    async def fake_run_live_loop(voice_io, **kwargs):  # noqa: ANN001, ANN003
        stop = kwargs["stop"]
        observed_stop["flag"] = stop
        started.set()
        while not stop.is_set():  # mirrors the real loop's exit condition
            await asyncio.sleep(0.01)

    original = live.run_live_loop
    live.run_live_loop = fake_run_live_loop
    try:
        daemon = _daemon(data_dir, voice=FakeVoiceIO())
        daemon._start_voice_loop(FakeVoiceIO())
        await asyncio.wait_for(asyncio.to_thread(started.wait, 2.0), 3.0)
        assert not observed_stop["flag"].is_set()

        await daemon.stop()

        assert observed_stop["flag"].is_set(), "stop() must signal the mic loop"
        assert daemon._voice_task is None
    finally:
        live.run_live_loop = original
