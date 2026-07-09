"""VoiceIO — bus-wired speak/wake orchestrator (spec 03 §VoiceIO).

Bridges the TTS adapter, the event bus, and injectable mic/wake/STT seams.
No heavy audio or cloud libraries are imported here; those live behind the
seam callables passed at construction time (defaults None for headless tests).

Speak path:
  1. sanitize_text (never-persist filter, §6.1) — redact secrets first.
  2. Hard 500-char cap — truncate + warning if exceeded.
  3. tts.speak(safe_text) — retain the handle for barge-in.

Wake path (_on_wake(transcript, confidence)):
  1. Barge-in — call stop() on the active TTS handle if one is live.
  2. Publish voice.wake (source="voice_io", payload={}).
  3. Publish voice.utterance (source="voice_io", payload={text, confidence})
     — UNLESS paused (utterance suppressed; wake is always published).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from edith.bus import EventBus
from edith.memory.secrets import sanitize_text
from edith.voice.tts import TTSAdapter, TTSHandle

_log = logging.getLogger(__name__)
_CHAR_CAP = 500
# Stuck-stream guard: if a TTS task never reports done() (e.g. a network stall on
# the ElevenLabs stream), is_speaking must not wedge True forever or the mic goes
# permanently deaf. Past this ceiling we abandon the handle. Generous — normal
# 1–2 sentence replies finish in well under this.
_MAX_SPEAK_SECONDS = 30.0


class VoiceIO:
    """Orchestrates TTS playback, barge-in, and bus event publishing."""

    def __init__(
        self,
        bus: EventBus,
        tts: TTSAdapter,
        *,
        mic_source: Callable[[], Any] | None = None,
        wake_detector: Callable[[], Any] | None = None,
        stt: Callable[[], Any] | None = None,
        max_speak_seconds: float = _MAX_SPEAK_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._bus = bus
        self._tts = tts
        # Injectable seams — real mic/openWakeWord/faster-whisper live here;
        # None means headless/test mode. Never imported at module top.
        self._mic_source = mic_source
        self._wake_detector = wake_detector
        self._stt = stt
        self._active_handle: TTSHandle | None = None
        self._speak_started = 0.0
        self._max_speak_seconds = max_speak_seconds
        self._clock = clock
        self._paused = False

    def set_paused(self, paused: bool) -> None:
        """Pause or unpause utterance publishing (voice.wake still fires when paused)."""
        self._paused = paused

    @property
    def is_speaking(self) -> bool:
        """True while TTS is (or should be) playing — the half-duplex mic gate.

        The live mic loop reads this to suppress wake detection during playback so
        EDITH never re-triggers on her own voice. Backed by the handle's ``done()``,
        with a stuck-stream ceiling so a stalled task can't leave the mic deaf.
        """
        handle = self._active_handle
        if handle is None or handle.done():
            return False
        if self._clock() - self._speak_started > self._max_speak_seconds:
            # Stall guard: abandon the wedged stream so the mic reopens.
            handle.stop()
            self._active_handle = None
            _log.warning("speak: abandoned a stuck TTS stream after %.0fs", self._max_speak_seconds)
            return False
        return True

    async def speak(self, text: str) -> None:
        """Redact → cap → speak via TTS adapter; retain handle for barge-in."""
        safe_text = sanitize_text(text)
        if len(safe_text) > _CHAR_CAP:
            _log.warning(
                "speak: text truncated from %d to %d chars", len(safe_text), _CHAR_CAP
            )
            safe_text = safe_text[:_CHAR_CAP]
        self._speak_started = self._clock()
        self._active_handle = await self._tts.speak(safe_text)

    async def _on_wake(self, transcript: str, confidence: float) -> None:
        """Handle a wake-word detection: barge-in → wake event → utterance event.

        Called by the wake detector seam (or directly in tests) with the
        recognised transcript and its confidence score.
        """
        # Barge-in: stop active TTS playback before doing anything else.
        if self._active_handle is not None:
            self._active_handle.stop()
            self._active_handle = None

        # Always publish the wake signal — even while paused.
        await self._bus.publish("voice.wake", source="voice_io", payload={})

        # Suppress the utterance while paused (privacy — don't capture this moment).
        if self._paused:
            return

        await self._bus.publish(
            "voice.utterance",
            source="voice_io",
            payload={"text": transcript, "confidence": confidence},
        )
