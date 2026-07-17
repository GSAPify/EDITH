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

import difflib
import logging
import re
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
# 1–2 sentence replies finish in well under this. Set high enough that a genuinely
# long reply (near the 500-char cap ≈ ~40s of speech) is never wrongly abandoned.
_MAX_SPEAK_SECONDS = 75.0
# Half-duplex "hangover": a TTS task reports done() when the last audio chunk is
# WRITTEN, but the output buffer keeps PLAYING for a beat after. Hold the mic gate
# closed this many seconds past done() so the mic can't hear the tail of EDITH's
# own voice and false-wake on it.
_SPEAK_COOLDOWN = 2.5
# Echo backstop (belt to the gate's braces): a transcript recognised within this
# window that matches something EDITH just said is her own voice leaking back.
_ECHO_WINDOW = 20.0
_ECHO_RATIO = 0.72


def _normalize(text: str) -> str:
    """Lowercase, punctuation→space, collapse whitespace — for echo comparison."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", text.lower())).strip()


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
        speak_cooldown: float = _SPEAK_COOLDOWN,
        echo_window: float = _ECHO_WINDOW,
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
        self._speak_cooldown = speak_cooldown
        self._echo_window = echo_window
        self._speaking_until = 0.0
        self._recent_spoken: list[tuple[float, str]] = []  # (ts, normalized text)
        self._clock = clock
        self._paused = False

    def set_paused(self, paused: bool) -> None:
        """Pause or unpause utterance publishing (voice.wake still fires when paused)."""
        self._paused = paused

    @property
    def is_paused(self) -> bool:
        """Whether utterance publishing is muted (read side of ``set_paused``).

        The live loop reads this to close the follow-up window and stop capturing
        while muted, so a muted owner doesn't run STT on ambient audio.
        """
        return self._paused

    @property
    def is_speaking(self) -> bool:
        """True while TTS is (or should be) playing — the half-duplex mic gate.

        The live mic loop reads this to suppress wake detection during playback so
        EDITH never re-triggers on her own voice. Backed by the handle's ``done()``,
        with a stuck-stream ceiling so a stalled task can't leave the mic deaf.
        """
        handle = self._active_handle
        now = self._clock()
        if handle is None:
            return False
        if not handle.done():
            if now - self._speak_started > self._max_speak_seconds:
                # Stall guard: abandon the wedged stream so the mic reopens.
                handle.stop()
                self._active_handle = None
                _log.warning(
                    "speak: abandoned a stuck TTS stream after %.0fs", self._max_speak_seconds
                )
                return False
            self._speaking_until = now + self._speak_cooldown  # extend hangover while streaming
            return True
        # Stream WRITTEN but the speaker buffer is still draining: hold the gate for
        # the cooldown so the mic never hears the tail of EDITH's own voice.
        if now < self._speaking_until:
            return True
        self._active_handle = None
        return False

    async def speak(self, text: str) -> None:
        """Redact → cap → speak via TTS adapter; retain handle for barge-in."""
        safe_text = sanitize_text(text)
        if len(safe_text) > _CHAR_CAP:
            _log.warning(
                "speak: text truncated from %d to %d chars", len(safe_text), _CHAR_CAP
            )
            safe_text = safe_text[:_CHAR_CAP]
        now = self._clock()
        self._speak_started = now
        self._speaking_until = now + self._speak_cooldown
        # Remember what we're about to say so a mic pickup of it can be filtered as
        # self-echo (see _is_echo). Redacted text is fine — it's only compared, not stored.
        self._recent_spoken.append((now, _normalize(safe_text)))
        self._active_handle = await self._tts.speak(safe_text)

    async def _on_wake(self, transcript: str, confidence: float) -> None:
        """Handle a wake-word detection: barge-in → wake event → utterance event.

        Called by the wake detector seam (or directly in tests) with the
        recognised transcript and its confidence score.
        """
        # Echo suppression FIRST (before barge-in): a transcript matching something
        # EDITH just said is her own TTS leaking into the mic — drop it silently so it
        # neither cuts her off (barge-in) nor loops (utterance). A real interruption
        # won't match her recent speech, so it passes straight through.
        if self._is_echo(transcript):
            _log.info("voice: suppressed self-echo %r", transcript[:60])
            return

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

    def _is_echo(self, transcript: str) -> bool:
        """True if ``transcript`` is EDITH's own recent speech leaking back into the mic.

        Compares (normalized) against what she said within the echo window: a containment
        either way (STT often catches a fragment) or a high fuzzy ratio (STT is imperfect)
        counts as an echo. Prunes stale entries as a side effect.
        """
        now = self._clock()
        self._recent_spoken = [
            (t, s) for (t, s) in self._recent_spoken if now - t <= self._echo_window
        ]
        cand = _normalize(transcript)
        if not cand:
            return False
        for _t, spoken in self._recent_spoken:
            if not spoken:
                continue
            if cand in spoken or spoken in cand:
                return True
            if difflib.SequenceMatcher(None, cand, spoken).ratio() >= _ECHO_RATIO:
                return True
        return False
