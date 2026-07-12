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
