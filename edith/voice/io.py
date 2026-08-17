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
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
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
_SPEAK_COOLDOWN = 0.3
# Echo backstop (belt to the gate's braces): a transcript recognised within this
# window that matches something EDITH just said is her own voice leaking back.
_ECHO_WINDOW = 20.0
_ECHO_RATIO = 0.72


def _normalize(text: str) -> str:
    """Lowercase, punctuation→space, collapse whitespace — for echo comparison."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", text.lower())).strip()


@dataclass
class _SpeechRecord:
    """One tracked TTS utterance (concurrency fix — see VoiceIO's speak/is_speaking).

    VoiceIO tracks a LIST of these, not a single handle/flag pair, so an earlier
    utterance's completion (and its follow-up signal, if it was a response) is never
    orphaned by a later, overlapping ``speak()``/``speak_response()`` call. ``handle``
    is ``None`` while the owning call is still awaiting ``tts.speak()`` — that call
    still counts as "speaking" (see ``VoiceIO.is_speaking``). ``generation`` pins the
    record to the barge-in epoch it was created in: if a barge-in bumps the epoch
    before this record's handle arrives, the late handle is stopped on arrival instead
    of being adopted (see ``VoiceIO._speak`` / ``VoiceIO._on_wake``). ``abandoned`` is
    the same idea for a record dropped by the max-speak ceiling while still pending
    (``handle is None``) — a global generation bump would also invalidate unrelated
    concurrent calls, so this is a per-record marker instead.
    """

    is_response: bool
    started: float
    speaking_until: float
    generation: int
    handle: TTSHandle | None = None
    abandoned: bool = False


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
        self._max_speak_seconds = max_speak_seconds
        self._speak_cooldown = speak_cooldown
        self._echo_window = echo_window
        self._recent_spoken: list[tuple[float, str]] = []  # (ts, normalized text)
        self._clock = clock
        self._paused = False
        # Every in-flight or recently-finished utterance (speak()/speak_response()), so
        # overlapping TTS (e.g. narration racing a response) never orphans an earlier
        # call's completion. Mutated from the event-loop thread (speak/_on_wake) AND read
        # from the mic worker thread (is_speaking/consume_followup_ready) — guarded by a
        # plain threading.Lock, never held across an ``await`` (never blocks the loop).
        self._records: list[_SpeechRecord] = []
        self._records_lock = threading.Lock()
        # Bumped on every barge-in; a record created before the bump whose handle is
        # still being awaited is stopped on arrival rather than adopted (see _speak).
        self._generation = 0
        # One-shot: set True once a speak_response() record genuinely finishes (cooldown
        # elapsed, stream done — not abandoned). Only actually surfaced by
        # consume_followup_ready() once EVERY tracked record is idle (is_speaking is
        # False) — an overlapping narration or a second response cannot erase it first.
        self._followup_ready = False

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
        """True while ANY tracked TTS call is (or should be) playing — the half-duplex
        mic gate. Stays True until EVERY tracked utterance's cooldown has elapsed, so
        overlapping speech (a response racing narration, or two overlapping responses)
        cannot let the mic reopen early.

        The live mic loop reads this to suppress wake detection during playback so
        EDITH never re-triggers on her own voice. Backed by each handle's ``done()``,
        with a stuck-stream ceiling so a stalled task can't leave the mic deaf.
        """
        now = self._clock()
        to_stop: list[TTSHandle] = []
        with self._records_lock:
            still_active: list[_SpeechRecord] = []
            finished_response = False
            for record in self._records:
                handle = record.handle
                if handle is None:
                    # tts.speak() for this call hasn't returned yet — still speaking,
                    # UNLESS it's been pending so long it hits the same stall ceiling as
                    # a wedged stream (a hung network call is indistinguishable from a
                    # hung stream from the mic's point of view). Marked (not just
                    # dropped) so a handle that arrives after this point is stopped
                    # immediately instead of being adopted — see VoiceIO._speak.
                    if now - record.started > self._max_speak_seconds:
                        record.abandoned = True
                        _log.warning(
                            "speak: abandoned a pending TTS call after %.0fs "
                            "(tts.speak() never returned a handle)",
                            self._max_speak_seconds,
                        )
                        continue
                    still_active.append(record)
                    continue
                if not handle.done():
                    if now - record.started > self._max_speak_seconds:
                        # Stall guard: abandon the wedged stream so the mic reopens. An
                        # abandoned stream never finished producing its reply, so it is
                        # dropped WITHOUT signalling a completed response.
                        to_stop.append(handle)
                        _log.warning(
                            "speak: abandoned a stuck TTS stream after %.0fs",
                            self._max_speak_seconds,
                        )
                        continue
                    record.speaking_until = now + self._speak_cooldown  # extend while streaming
                    still_active.append(record)
                    continue
                # Stream WRITTEN but the speaker buffer may still be draining: hold the
                # gate for the cooldown so the mic never hears the tail of EDITH's voice.
                if now < record.speaking_until:
                    still_active.append(record)
                    continue
                # Genuine completion: done() AND cooldown elapsed for THIS record.
                if record.is_response:
                    finished_response = True
            self._records = still_active
            if finished_response:
                self._followup_ready = True
            still_speaking = bool(still_active)
        for handle in to_stop:  # stop() can block (subprocess/stream teardown) — never under lock
            handle.stop()
        return still_speaking

    def consume_followup_ready(self) -> bool:
        """One-shot: True iff a response genuinely finished AND every tracked utterance
        is now idle (``is_speaking`` would return False).

        Read by the live mic loop once per poll (``edith.voice.live``) to decide whether
        to open the follow-up ``ConversationWindow`` — including on a poll where the
        speaking→idle edge itself was missed (see that module's docstring). While any
        other utterance (e.g. overlapping narration, or a second response) is still
        active, the signal is RETAINED rather than lost or exposed early. Reading it
        once everything is idle clears the flag, so a completion opens the window once.
        """
        with self._records_lock:
            if self._records:  # something is still tracked/active — not idle yet
                return False
            ready = self._followup_ready
            self._followup_ready = False
            return ready

    async def speak(self, text: str) -> None:
        """Redact → cap → speak via TTS adapter; retain handle for barge-in.

        Ordinary narration — the startup greeting, session narration — never arms
        follow-up. Use ``speak_response`` for a reply to the owner (spec 03 §Follow-up).
        """
        await self._speak(text, is_response=False)

    async def speak_response(self, text: str) -> None:
        """Speak a user-facing response and arm follow-up when it genuinely finishes.

        Every response the owner should be able to talk back to — a skill's
        acknowledgement AND its final reply, a plain Brain answer, a background-
        reasoning ping — must go through here. Each call re-arms independently, so
        an acknowledgement's completion can't steal the final reply's follow-up window.
        """
        await self._speak(text, is_response=True)

    async def _speak(self, text: str, *, is_response: bool) -> None:
        """Shared speak path: redact → cap → speak via TTS adapter; track the resulting
        handle for barge-in and the half-duplex gate. ``is_response`` decides whether
        this utterance can arm follow-up.

        A record is registered BEFORE awaiting ``tts.speak()`` (with ``handle=None``) so
        the mic loop sees this call as "speaking" for its entire duration, and so a
        barge-in that lands while the call is still in flight can be honored the instant
        the handle finally arrives (see the ``generation``/``abandoned`` check below)
        instead of racing an ``_active_handle`` write that hasn't happened yet. If
        ``tts.speak()`` raises OR this await is cancelled, the record is removed before
        propagating — an untracked pending record would otherwise gate the mic forever.
        """
        safe_text = sanitize_text(text)
        if len(safe_text) > _CHAR_CAP:
            _log.warning(
                "speak: text truncated from %d to %d chars", len(safe_text), _CHAR_CAP
            )
            safe_text = safe_text[:_CHAR_CAP]
        now = self._clock()
        with self._records_lock:
            generation = self._generation
            record = _SpeechRecord(
                is_response=is_response,
                started=now,
                speaking_until=now + self._speak_cooldown,
                generation=generation,
            )
            self._records.append(record)
        # Remember what we're about to say so a mic pickup of it can be filtered as
        # self-echo (see _is_echo). Redacted text is fine — it's only compared, not stored.
        self._recent_spoken.append((now, _normalize(safe_text)))
        try:
            handle = await self._tts.speak(safe_text)
        except BaseException:
            with self._records_lock:
                self._records = [r for r in self._records if r is not record]
            raise
        stop_now = False
        with self._records_lock:
            if generation == self._generation and not record.abandoned:
                record.handle = handle
            else:
                # A barge-in landed (generation) or the max-speak ceiling already
                # abandoned this pending call (abandoned) while we were awaiting the
                # handle. Stop it after releasing the lock so it never starts playing
                # untracked / under an interruption.
                stop_now = True
        if stop_now:
            handle.stop()

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

        # Barge-in: stop EVERY tracked TTS handle before doing anything else — an
        # earlier overlapping utterance must not be left orphaned and still playing. An
        # interrupted utterance never finished, so none of them are reported as a
        # completed response. Bump the generation so a call still awaiting its handle
        # (record.handle is None) gets stopped the instant it arrives (see _speak). Also
        # clear any already-armed but not-yet-consumed follow-up signal — a completion
        # from BEFORE the interruption must not leak a follow-up window open for this
        # new (barged-in) turn.
        with self._records_lock:
            records, self._records = self._records, []
            self._generation += 1
            self._followup_ready = False
        for record in records:
            if record.handle is not None:
                record.handle.stop()

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
