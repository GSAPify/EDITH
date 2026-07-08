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
    """Records stop() calls; satisfies TTSHandle protocol."""

    def __init__(self) -> None:
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


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
