"""Tests for VoiceIO (spec 03 §VoiceIO): headless, fakes for bus/tts/handle.

TDD red->green: tests drive the implementation in edith/voice/io.py.

Five cases:
  1. speak() redacts a planted secret before TTS sees it (non-vacuous).
  2. speak() truncates text > 500 chars.
  3. _on_wake() publishes voice.wake then voice.utterance (in order).
  4. _on_wake() while TTS is active calls stop() on the live handle (barge-in).
  5. set_paused(True) suppresses voice.utterance but NOT voice.wake.
"""

from __future__ import annotations

import asyncio

import pytest

from edith.bus import Event, EventBus
from edith.voice.io import VoiceIO
from edith.voice.tts import TTSAdapter, TTSHandle

# ---------------------------------------------------------------------------
# Fakes — no real audio, no real cloud deps
# ---------------------------------------------------------------------------


class _FakeHandle:
    """Records stop() calls + a controllable done() flag; satisfies TTSHandle."""

    def __init__(self, done: bool = False) -> None:
        self.stopped = False
        self._done = done

    def stop(self) -> None:
        self.stopped = True
        self._done = True

    def done(self) -> bool:
        return self._done


class _FakeTTS(TTSAdapter):
    """Records the text passed to speak() and returns a controllable handle."""

    def __init__(self) -> None:
        self.received: list[str] = []
        self._handle = _FakeHandle()

    def name(self) -> str:
        return "fake"

    async def speak(self, text: str) -> TTSHandle:  # type: ignore[override]
        self.received.append(text)
        return self._handle  # type: ignore[return-value]


class _SequencedTTS(TTSAdapter):
    """Returns pre-configured handles in call order; a given call index can be gated
    (held open on an ``asyncio.Event``) so tests can deterministically interleave two
    overlapping speak()/speak_response() calls without any real audio or concurrency."""

    def __init__(
        self, handles: list[TTSHandle], gates: dict[int, asyncio.Event] | None = None
    ) -> None:
        self.received: list[str] = []
        self._handles = handles
        self._gates = gates or {}

    def name(self) -> str:
        return "fake-sequenced"

    async def speak(self, text: str) -> TTSHandle:  # type: ignore[override]
        index = len(self.received)
        self.received.append(text)
        gate = self._gates.get(index)
        if gate is not None:
            await gate.wait()
        return self._handles[index]


class _RaisingTTS(TTSAdapter):
    """speak() raises immediately — for testing pending-record cleanup on failure."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def name(self) -> str:
        return "fake-raising"

    async def speak(self, text: str) -> TTSHandle:  # type: ignore[override]
        raise self._exc


class _HangingTTS(TTSAdapter):
    """speak() awaits an external gate that never fires on its own — for testing
    cancellation and the pending-handle-creation timeout. Releasing ``gate`` lets the
    call finally "return" ``handle``, simulating a very slow (but not cancelled) TTS
    backend."""

    def __init__(self) -> None:
        self.gate: asyncio.Event = asyncio.Event()
        self.handle: TTSHandle = _FakeHandle()  # type: ignore[assignment]

    def name(self) -> str:
        return "fake-hanging"

    async def speak(self, text: str) -> TTSHandle:  # type: ignore[override]
        await self.gate.wait()
        return self.handle


def _make_bus_spy() -> tuple[EventBus, list[Event]]:
    """Return (bus, captured_events) — collects voice.wake + voice.utterance."""
    bus = EventBus()
    events: list[Event] = []

    async def _capture(event: Event) -> None:
        events.append(event)

    bus.subscribe("voice.wake", _capture)
    bus.subscribe("voice.utterance", _capture)
    return bus, events


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_speak_redacts_secret_before_tts() -> None:
    """speak() must sanitize text BEFORE handing it to the TTS adapter.

    NON-VACUOUS: asserts the raw string contains the secret AND the adapter
    never received it.
    """
    raw = "Your key is sk-bf-DEADBEEF, use it wisely."
    assert "sk-bf-DEADBEEF" in raw, "precondition: secret is present in raw input"

    tts = _FakeTTS()
    bus, _ = _make_bus_spy()
    vio = VoiceIO(bus=bus, tts=tts)
    await vio.speak(raw)

    assert tts.received, "TTS adapter was never called"
    assert "sk-bf-DEADBEEF" not in tts.received[0], "Secret leaked to TTS adapter"


async def test_speak_truncates_at_500_chars() -> None:
    """speak() must truncate text exceeding 500 chars before calling TTS."""
    long_text = "a" * 600
    tts = _FakeTTS()
    bus, _ = _make_bus_spy()
    vio = VoiceIO(bus=bus, tts=tts)
    await vio.speak(long_text)

    assert tts.received, "TTS adapter was never called"
    assert len(tts.received[0]) == 500


async def test_wake_publishes_wake_then_utterance() -> None:
    """_on_wake() publishes voice.wake before voice.utterance, with correct payload."""
    tts = _FakeTTS()
    bus, events = _make_bus_spy()
    vio = VoiceIO(bus=bus, tts=tts)
    await vio._on_wake("hello world", 0.95)

    topics = [e.topic for e in events]
    assert "voice.wake" in topics, "voice.wake not published"
    assert "voice.utterance" in topics, "voice.utterance not published"
    assert topics.index("voice.wake") < topics.index("voice.utterance"), (
        "voice.wake must be published before voice.utterance"
    )

    utterance_event = next(e for e in events if e.topic == "voice.utterance")
    assert utterance_event.payload["text"] == "hello world"
    assert utterance_event.payload["confidence"] == pytest.approx(0.95)
    assert utterance_event.source == "voice_io"


async def test_barge_in_stops_active_handle() -> None:
    """_on_wake() while TTS is active must call stop() on the live handle."""
    tts = _FakeTTS()
    bus, _ = _make_bus_spy()
    vio = VoiceIO(bus=bus, tts=tts)

    # Arm an active TTS playback so there is a live handle.
    await vio.speak("I am currently speaking")
    handle = tts._handle
    assert not handle.stopped, "precondition: handle not yet stopped"

    # A new wake event should barge in.
    await vio._on_wake("stop that", 0.9)
    assert handle.stopped, "Barge-in did not call stop() on the active handle"


async def test_paused_suppresses_utterance_but_not_wake() -> None:
    """While paused, _on_wake() publishes voice.wake but NOT voice.utterance."""
    tts = _FakeTTS()
    bus, events = _make_bus_spy()
    vio = VoiceIO(bus=bus, tts=tts)
    vio.set_paused(True)

    await vio._on_wake("private conversation", 0.88)

    topics = [e.topic for e in events]
    assert "voice.wake" in topics, "voice.wake must always fire"
    assert "voice.utterance" not in topics, "utterance must be suppressed while paused"


async def test_is_speaking_holds_through_cooldown_after_done() -> None:
    """is_speaking stays True for the cooldown AFTER done() — the audio buffer keeps
    playing past stream-write, so the gate must outlast done() to block the TTS tail."""
    now = [1000.0]
    tts = _FakeTTS()
    bus, _ = _make_bus_spy()
    vio = VoiceIO(bus=bus, tts=tts, speak_cooldown=2.5, clock=lambda: now[0])

    assert vio.is_speaking is False  # nothing spoken yet
    await vio.speak("talking now")
    assert vio.is_speaking is True  # streaming
    tts._handle._done = True  # stream WRITTEN, but buffer still draining
    assert vio.is_speaking is True  # cooldown holds the gate closed
    now[0] += 3.0  # past the cooldown
    assert vio.is_speaking is False  # gate finally opens


async def test_stuck_stream_guard_frees_the_mic() -> None:
    """A wedged TTS task (never done) must not keep the mic gated forever."""
    now = [1000.0]
    tts = _FakeTTS()
    bus, _ = _make_bus_spy()
    vio = VoiceIO(bus=bus, tts=tts, max_speak_seconds=30.0, clock=lambda: now[0])

    await vio.speak("this stream will stall and never report done")
    assert vio.is_speaking is True
    now[0] += 31.0  # past the stall ceiling
    assert vio.is_speaking is False  # guard released the gate
    assert tts._handle.stopped is True  # and abandoned the wedged stream


async def test_on_wake_suppresses_self_echo() -> None:
    """An utterance matching what EDITH just said is dropped — no barge-in, no utterance."""
    tts = _FakeTTS()
    bus, events = _make_bus_spy()
    vio = VoiceIO(bus=bus, tts=tts)

    await vio.speak("I'm doing great, thanks for asking!")
    events.clear()

    # The mic picks up her own tail (STT catches a fragment) → must be suppressed.
    await vio._on_wake("I'm doing great thanks for asking", 0.75)
    assert [e.topic for e in events] == []  # neither voice.wake nor voice.utterance fired
    assert tts._handle.stopped is False  # echo must NOT barge-in on her own speech


async def test_on_wake_lets_a_real_interruption_through() -> None:
    """A genuine new utterance (not matching recent speech) is NOT treated as echo."""
    tts = _FakeTTS()
    bus, events = _make_bus_spy()
    vio = VoiceIO(bus=bus, tts=tts)

    await vio.speak("I'm doing great, thanks for asking!")
    events.clear()

    await vio._on_wake("what's the weather in Tokyo", 0.9)
    topics = [e.topic for e in events]
    assert "voice.utterance" in topics  # real query passes through


# ---------------------------------------------------------------------------
# Response-completion signal (bounded voice behavior fix)
# ---------------------------------------------------------------------------


async def test_default_speak_cooldown_is_point_three_seconds() -> None:
    """The shipping half-duplex cooldown defaults to 0.3s (down from 2.5s)."""
    now = [1000.0]
    tts = _FakeTTS()
    bus, _ = _make_bus_spy()
    vio = VoiceIO(bus=bus, tts=tts, clock=lambda: now[0])  # no speak_cooldown override

    await vio.speak("talking now")
    tts._handle._done = True
    assert vio.is_speaking is True  # still inside the default cooldown
    now[0] += 0.31
    assert vio.is_speaking is False  # default cooldown (0.3s) has elapsed


async def test_speak_response_signals_followup_when_it_genuinely_finishes() -> None:
    """speak_response() arms follow-up; consume_followup_ready() fires once, on completion."""
    now = [1000.0]
    tts = _FakeTTS()
    bus, _ = _make_bus_spy()
    vio = VoiceIO(bus=bus, tts=tts, speak_cooldown=0.3, clock=lambda: now[0])

    await vio.speak_response("here's your answer")
    assert vio.consume_followup_ready() is False  # still speaking — not ready yet
    tts._handle._done = True
    assert vio.consume_followup_ready() is False  # done() but cooldown not elapsed yet
    now[0] += 0.31  # past cooldown
    assert vio.is_speaking is False  # completion is observed via is_speaking
    assert vio.consume_followup_ready() is True  # signalled exactly once
    assert vio.consume_followup_ready() is False  # one-shot: cleared on read


async def test_ordinary_speak_never_arms_followup() -> None:
    """speak() (startup greeting / session narration) must never signal follow-up."""
    now = [1000.0]
    tts = _FakeTTS()
    bus, _ = _make_bus_spy()
    vio = VoiceIO(bus=bus, tts=tts, speak_cooldown=0.3, clock=lambda: now[0])

    await vio.speak("Voice loop online. Say hey jarvis to talk to me.")
    tts._handle._done = True
    now[0] += 0.31
    assert vio.is_speaking is False
    assert vio.consume_followup_ready() is False  # narration never arms follow-up


async def test_acknowledgement_then_final_response_each_rearm_followup() -> None:
    """An ack speak_response() then a final speak_response() each independently signal —
    the ack's completion cannot steal the final reply's follow-up window."""
    now = [1000.0]
    tts = _FakeTTS()
    bus, _ = _make_bus_spy()
    vio = VoiceIO(bus=bus, tts=tts, speak_cooldown=0.3, clock=lambda: now[0])

    await vio.speak_response("Fetching Tavishi's PR, reviewing now...")
    tts._handle._done = True
    now[0] += 0.31
    assert vio.is_speaking is False
    assert vio.consume_followup_ready() is True  # the ack rearms and is consumed

    # A second, later response (the final reply) must ALSO signal — the ack's
    # already-consumed signal does not linger or get reused.
    tts._handle = _FakeHandle()
    await vio.speak_response("Reviewed the PR. Looks clean.")
    assert vio.consume_followup_ready() is False  # not finished yet
    tts._handle._done = True
    now[0] += 0.31
    assert vio.is_speaking is False
    assert vio.consume_followup_ready() is True  # the final reply rearms independently


async def test_barge_in_does_not_signal_a_completed_response() -> None:
    """A response interrupted by a new wake (barge-in) must NOT report as completed."""
    tts = _FakeTTS()
    bus, _ = _make_bus_spy()
    vio = VoiceIO(bus=bus, tts=tts)

    await vio.speak_response("still speaking when interrupted")
    await vio._on_wake("stop that", 0.9)  # barge-in stops the handle
    assert vio.consume_followup_ready() is False  # abandoned mid-response — no signal


async def test_stuck_stream_abandonment_does_not_signal_a_completed_response() -> None:
    """A wedged TTS stream abandoned past the stall ceiling must NOT report completion."""
    now = [1000.0]
    tts = _FakeTTS()
    bus, _ = _make_bus_spy()
    vio = VoiceIO(bus=bus, tts=tts, max_speak_seconds=30.0, clock=lambda: now[0])

    await vio.speak_response("this stream will stall and never report done")
    assert vio.is_speaking is True
    now[0] += 31.0  # past the stall ceiling
    assert vio.is_speaking is False  # guard released the gate
    assert vio.consume_followup_ready() is False  # abandonment is not a completed response


# ---------------------------------------------------------------------------
# Overlapping / concurrent speech (round-2 concurrency fix)
# ---------------------------------------------------------------------------


async def test_narration_finishing_while_response_handle_creation_pending_does_not_arm() -> None:
    """Regression for the cross-thread race: a delayed speak_response() must not let a
    DIFFERENT (narration) handle's completion falsely arm follow-up while the response's
    own handle is still being created (its ``await tts.speak()`` has not returned yet)."""
    now = [1000.0]
    response_gate = asyncio.Event()
    response_handle = _FakeHandle(done=False)
    narration_handle = _FakeHandle(done=True)  # narration finishes immediately
    tts = _SequencedTTS([response_handle, narration_handle], gates={0: response_gate})
    bus, _ = _make_bus_spy()
    vio = VoiceIO(bus=bus, tts=tts, speak_cooldown=0.3, clock=lambda: now[0])

    # Kick off the response FIRST — its tts.speak() call blocks on response_gate, so its
    # handle has not been assigned to VoiceIO yet.
    response_task = asyncio.create_task(vio.speak_response("delayed reply"))
    await asyncio.sleep(0)  # let it register and reach the gate

    # Ordinary narration starts and runs to completion while the response is still pending.
    await vio.speak("unrelated narration")
    now[0] += 0.31  # past narration's cooldown
    assert vio.is_speaking is True  # the response's in-flight call still counts as speaking
    assert vio.consume_followup_ready() is False  # narration must not arm; response isn't done

    # Release the response and let its handle get created + finish.
    response_gate.set()
    await response_task
    response_handle._done = True
    now[0] += 0.31  # past the response's cooldown
    assert vio.is_speaking is False
    assert vio.consume_followup_ready() is True  # the ACTUAL response completing does arm


async def test_response_completion_overlapped_by_narration_retains_signal_until_idle() -> None:
    """A response finishes first but overlapping narration is still speaking — is_speaking
    must stay True, and the one-shot follow-up signal must be RETAINED (not lost) until
    every tracked speech is idle."""
    now = [1000.0]
    response_handle = _FakeHandle(done=True)  # the response finishes almost immediately
    narration_handle = _FakeHandle(done=False)  # narration is still playing
    tts = _SequencedTTS([response_handle, narration_handle])
    bus, _ = _make_bus_spy()
    vio = VoiceIO(bus=bus, tts=tts, speak_cooldown=0.3, clock=lambda: now[0])

    await vio.speak_response("quick ack")
    await vio.speak("overlapping narration, still going")
    now[0] += 0.31  # past the response's cooldown; narration is still not done()

    assert vio.is_speaking is True  # narration is still tracked as active
    assert vio.consume_followup_ready() is False  # not idle yet — signal is retained, not lost

    narration_handle._done = True
    now[0] += 0.31  # past narration's cooldown too
    assert vio.is_speaking is False  # everything is now idle
    assert vio.consume_followup_ready() is True  # the retained signal is finally delivered


async def test_overlapping_ack_and_final_response_do_not_erase_each_others_signal() -> None:
    """Two overlapping speak_response() calls (e.g. a skill's ack racing its own final
    reply) must not erase each other's follow-up intent — the window opens once, after
    ALL active output goes idle."""
    now = [1000.0]
    ack_handle = _FakeHandle(done=True)  # the ack finishes first
    final_handle = _FakeHandle(done=False)  # the final reply is still speaking
    tts = _SequencedTTS([ack_handle, final_handle])
    bus, _ = _make_bus_spy()
    vio = VoiceIO(bus=bus, tts=tts, speak_cooldown=0.3, clock=lambda: now[0])

    await vio.speak_response("Fetching your PR, reviewing now...")  # the ack
    await vio.speak_response("Reviewed the PR. Looks clean.")  # the final reply, overlapping

    now[0] += 0.31  # past the ack's cooldown; the final reply is still not done()
    assert vio.is_speaking is True  # the final reply keeps the gate closed
    assert vio.consume_followup_ready() is False  # ack's signal retained, not exposed yet

    final_handle._done = True
    now[0] += 0.31  # past the final reply's cooldown too
    assert vio.is_speaking is False
    assert vio.consume_followup_ready() is True  # opens exactly once, after all output is idle
    assert vio.consume_followup_ready() is False  # one-shot even across two contributing responses


async def test_barge_in_stops_every_tracked_handle() -> None:
    """Barge-in while TWO utterances overlap must stop BOTH handles, not just the most
    recently started one — an orphaned handle would keep playing past the interruption."""
    first_handle = _FakeHandle()
    second_handle = _FakeHandle()
    tts = _SequencedTTS([first_handle, second_handle])
    bus, _ = _make_bus_spy()
    vio = VoiceIO(bus=bus, tts=tts)

    await vio.speak_response("first, still speaking")
    await vio.speak("second, overlapping narration")

    await vio._on_wake("stop both of you", 0.9)

    assert first_handle.stopped is True
    assert second_handle.stopped is True
    assert vio.consume_followup_ready() is False  # neither is a completed response


# ---------------------------------------------------------------------------
# Pending-record cleanup (round-3 fix: a not-yet-handled record must never be
# left tracked/gating the mic forever on failure, cancellation, or a stall)
# ---------------------------------------------------------------------------


async def test_speak_exception_removes_the_pending_record() -> None:
    """If tts.speak() raises, the exception must propagate AND the pending record must
    not be left tracked forever — an untracked failure would permanently gate the mic."""
    bus, _ = _make_bus_spy()
    tts = _RaisingTTS(RuntimeError("tts backend unavailable"))
    vio = VoiceIO(bus=bus, tts=tts)

    with pytest.raises(RuntimeError, match="tts backend unavailable"):
        await vio.speak_response("this will fail")

    assert vio.is_speaking is False
    assert vio.consume_followup_ready() is False


async def test_speak_cancellation_removes_the_pending_record() -> None:
    """Cancelling a speak_response() call while it awaits tts.speak() must not leave the
    pending record gating the mic forever."""
    bus, _ = _make_bus_spy()
    tts = _HangingTTS()
    vio = VoiceIO(bus=bus, tts=tts)

    task = asyncio.create_task(vio.speak_response("this will be cancelled"))
    await asyncio.sleep(0)  # let it register the record and reach the gate
    assert vio.is_speaking is True  # the pending call is tracked

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert vio.is_speaking is False  # the mic must not stay gated forever
    assert vio.consume_followup_ready() is False


async def test_barge_in_clears_a_pending_followup_ready_signal() -> None:
    """A response finishes (arming follow-up) but the signal hasn't been consumed yet
    when a NEW wake barges in — the stale completion must not survive the interruption
    and leak a follow-up window open for a turn it was never meant for."""
    now = [1000.0]
    tts = _FakeTTS()
    tts._handle = _FakeHandle(done=True)
    bus, _ = _make_bus_spy()
    vio = VoiceIO(bus=bus, tts=tts, speak_cooldown=0.3, clock=lambda: now[0])

    await vio.speak_response("quick answer")
    now[0] += 0.31
    assert vio.is_speaking is False  # completion observed; _followup_ready armed internally

    await vio._on_wake("actually wait", 0.9)  # barge-in before consume_followup_ready() runs

    assert vio.consume_followup_ready() is False  # stale signal must not survive the interruption


async def test_pending_handle_creation_past_ceiling_is_abandoned_and_late_handle_stopped() -> None:
    """A speak_response() call whose tts.speak() never returns within max_speak_seconds
    must not gate the mic forever — and if the handle eventually DOES arrive, it must be
    stopped immediately rather than adopted (never signalling a completed response)."""
    now = [1000.0]
    tts = _HangingTTS()
    bus, _ = _make_bus_spy()
    vio = VoiceIO(bus=bus, tts=tts, max_speak_seconds=30.0, clock=lambda: now[0])

    task = asyncio.create_task(vio.speak_response("stuck before a handle even exists"))
    await asyncio.sleep(0)  # let it register the record and reach the gate
    assert vio.is_speaking is True  # pending call still counts as speaking

    now[0] += 31.0  # past the ceiling
    assert vio.is_speaking is False  # abandoned — the mic must reopen

    # The delayed tts.speak() call finally "returns" its handle.
    tts.gate.set()
    await task
    assert tts.handle.stopped is True  # type: ignore[attr-defined]  # stopped, never adopted
    assert vio.is_speaking is False
    assert vio.consume_followup_ready() is False  # never signals a completed response
