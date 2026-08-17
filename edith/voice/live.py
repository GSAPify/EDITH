"""Live audio wiring for VoiceIO (spec 03 §Audio pipeline, build steps 2-4).

This is the **hardware-facing** half of Slice 3: the real mic-capture →
openWakeWord → faster-whisper loop that drives ``VoiceIO`` on a live machine, and
a factory that assembles a ``VoiceIO`` with a real TTS adapter from config.

It is deliberately isolated from ``io.py`` (the tested core) because NONE of it
can be verified headlessly — it needs a microphone, a speaker, and (for
ElevenLabs) an API key + network. Every heavy import (``sounddevice``,
``openwakeword``, ``faster_whisper``) is done INSIDE a function so
``import edith.voice`` still works without the ``[voice]`` optional extra.

**Status: written against the installed SDK APIs (elevenlabs 2.56, openwakeword,
faster-whisper, sounddevice 0.5), NOT run against real hardware.** Expect to
debug on first live use — this is the owner live-smoke surface. The conversation-mode
wiring below (follow-up window + energy endpointing) is likewise owner-smoke-only; the
DECISION logic it calls lives in tested pure units (``ConversationWindow`` in
``conversation.py``, ``Endpointer`` in ``endpointing.py``). Barge-in still fires when
``_on_wake`` runs (after capture) rather than at the instant of wake-word detection.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import threading
from collections import deque
from collections.abc import Sequence
from typing import Any, Protocol

from edith.bus import EventBus
from edith.voice.adapters import select_adapter
from edith.voice.conversation import ConversationWindow
from edith.voice.endpointing import Endpointer
from edith.voice.io import VoiceIO
from edith.voice.tts import TTSAdapter

_log = logging.getLogger(__name__)

# 16 kHz mono is what both openWakeWord and faster-whisper expect. 1280 samples
# = 80 ms, openWakeWord's native frame size.
_SAMPLE_RATE = 16000
_FRAME_SAMPLES = 1280
_WAKE_MODEL = "hey_jarvis"  # openWakeWord bundles hey_jarvis_v0.1.onnx
_WAKE_THRESHOLD = 0.5
_FOLLOWUP_SECONDS = 10.0  # mic stays hot this long after a reply (follow-up, no wake)
# Residual TTS-tail drain after EDITH stops speaking, before the wake detector resumes.
# Shipping half-duplex (no VPIO/Speex AEC): kept short now that VoiceIO's own cooldown
# (see edith.voice.io._SPEAK_COOLDOWN) already absorbs most of the output-buffer hangover.
# Env-tunable for live calibration (no recompile).
_SPEAK_FLUSH_SECONDS = 0.3
# Energy endpointing (replaces the old fixed 5 s capture): end an utterance on
# trailing silence or a hard cap. Env-tunable for live calibration (no recompile).
_ENDPOINT_SILENCE_MS = 800.0
_ENDPOINT_MAX_MS = 15000.0
_ENDPOINT_THRESHOLD = 500.0  # RMS onset/silence threshold — CALIBRATE via EDITH_VOICE_DEBUG
# Frames retained before a trigger so a word's quiet onset is not clipped. 4 x 80 ms = 320 ms,
# comfortably longer than the 100-300 ms a word takes to ramp past the threshold.
_PREROLL_FRAMES = 4


def resolve_wake_model() -> str:
    """The wake model to listen for.

    A bundled openWakeWord name (``hey_jarvis``, ``alexa``, …) OR a path to a
    custom ``.onnx`` — e.g. a trained ``hey_edith`` model. ``EDITH_WAKE_MODEL``
    overrides the default. openWakeWord does NOT ship a ``hey_edith`` model, so
    "Hey EDITH" requires training one and pointing this at it.
    """
    return os.environ.get("EDITH_WAKE_MODEL", _WAKE_MODEL)


def wake_phrase(model: str) -> str:
    """Human phrasing of a wake model for prompts (``hey_jarvis`` → ``Hey Jarvis``)."""
    stem = os.path.basename(model).split(".")[0]  # /x/hey_edith.onnx -> hey_edith
    stem = stem.split("_v")[0]  # hey_jarvis_v0.1 -> hey_jarvis
    return stem.replace("_", " ").replace("-", " ").title()


def build_tts_adapter(
    *,
    engine: str | None = None,
    api_key: str | None = None,
    voice_id: str | None = None,
) -> TTSAdapter:
    """Build the configured TTS adapter from args or environment.

    ``TTS_ENGINE`` (default ``piper``) selects the engine; ElevenLabs also reads
    ``ELEVENLABS_API_KEY`` / ``ELEVENLABS_VOICE_ID``, and Piper reads
    ``PIPER_MODEL`` (path to a voice ``.onnx`` — Piper cannot run without one).
    Secrets come from the env (Keychain / ``.env``) and are never logged.
    """
    engine = engine or os.environ.get("TTS_ENGINE", "piper")
    if engine == "elevenlabs":
        return select_adapter(
            "elevenlabs",
            api_key=api_key or os.environ.get("ELEVENLABS_API_KEY", ""),
            voice_id=voice_id or os.environ.get("ELEVENLABS_VOICE_ID", ""),
        )
    return select_adapter(engine, model_path=os.environ.get("PIPER_MODEL", ""))


def build_live_voice_io(bus: EventBus, **adapter_kwargs: str) -> VoiceIO:
    """Assemble a ``VoiceIO`` with a real TTS adapter (mic/wake/STT come via run)."""
    return VoiceIO(bus, build_tts_adapter(**adapter_kwargs))


async def run_live_loop(
    voice_io: VoiceIO,
    *,
    wake_model: str = _WAKE_MODEL,
    wake_threshold: float = _WAKE_THRESHOLD,
    stt_model: str = "small.en",
    followup_seconds: float = _FOLLOWUP_SECONDS,
    stop: threading.Event | None = None,
) -> None:
    """Always-listening loop: mic → (wake | follow-up) → endpointed STT → publish.

    Runs the blocking audio loop in a worker thread and bridges each recognised
    utterance back onto the event loop via ``run_coroutine_threadsafe``.

    Set *stop* to end the loop cooperatively. ``asyncio.to_thread`` cannot interrupt a
    worker, and cancelling the task around it does not stop the thread — so without this
    the mic thread outlived shutdown, still holding an open PortAudio stream, and the
    interpreter tore down underneath it: Ctrl-C ended in a segfault. Checked once per
    frame (~80 ms), which is also how long a clean stop takes.
    """
    loop = asyncio.get_running_loop()
    await asyncio.to_thread(
        _blocking_listen,
        voice_io,
        loop,
        wake_model,
        wake_threshold,
        stt_model,
        followup_seconds,
        stop,
    )


def _blocking_listen(
    voice_io: VoiceIO,
    loop: asyncio.AbstractEventLoop,
    wake_model: str,
    wake_threshold: float,
    stt_model: str,
    followup_seconds: float,
    stop: threading.Event | None = None,
) -> None:
    """The blocking mic loop — runs in a worker thread (heavy imports here).

    Returns when *stop* is set, which exits the ``RawInputStream`` context and closes
    the PortAudio stream before the interpreter tears down (see ``run_live_loop``).
    """
    import numpy as np
    import sounddevice as sd
    from faster_whisper import WhisperModel
    from openwakeword.model import Model

    wake = Model(wakeword_models=[wake_model])
    stt = WhisperModel(stt_model, device="cpu", compute_type="int8")
    _log.info("VoiceIO live loop up: wake=%s stt=%s", wake_model, stt_model)

    debug = os.environ.get("EDITH_VOICE_DEBUG") == "1"
    # Half-duplex gate: while EDITH speaks, discard mic frames; when she stops,
    # flush the residual TTS tail and RESET the detector (the primary defense —
    # a sub-second leaked fragment can't complete a ~1.5 s wake phrase after a
    # reset). Flush length is env-tunable for the live retest (no recompile).
    flush_seconds = float(os.environ.get("EDITH_SPEAK_FLUSH_SECONDS", str(_SPEAK_FLUSH_SECONDS)))
    flush_frames = _frames_for_seconds(
        flush_seconds, sample_rate=_SAMPLE_RATE, frame_samples=_FRAME_SAMPLES
    )
    # Conversation mode: after a reply, accept a follow-up (no wake word) for a
    # window; end each utterance on trailing silence instead of a fixed clock. Both
    # are pure, unit-tested units — this loop only feeds them frames.
    window = ConversationWindow(window_seconds=followup_seconds)
    endpoint_threshold = float(os.environ.get("EDITH_ENDPOINT_THRESHOLD", str(_ENDPOINT_THRESHOLD)))
    endpointer = Endpointer(
        silence_ms=float(os.environ.get("EDITH_ENDPOINT_SILENCE_MS", str(_ENDPOINT_SILENCE_MS))),
        hard_max_ms=float(os.environ.get("EDITH_ENDPOINT_MAX_MS", str(_ENDPOINT_MAX_MS))),
        threshold=endpoint_threshold,
        frame_ms=_FRAME_SAMPLES / _SAMPLE_RATE * 1000.0,
    )
    # Pre-roll ring: the last few frames before a trigger, so a word's opening consonants
    # (which ramp up below the threshold) are not lost. See _capture_endpointed.
    preroll_frames = int(os.environ.get("EDITH_PREROLL_FRAMES", str(_PREROLL_FRAMES)))
    preroll: deque[Any] = deque(maxlen=max(0, preroll_frames))
    was_speaking = False
    n_frames, peak = 0, 0.0
    with sd.RawInputStream(
        samplerate=_SAMPLE_RATE, channels=1, dtype="int16", blocksize=_FRAME_SAMPLES
    ) as stream:
        while stop is None or not stop.is_set():
            action, was_speaking = _gate_action(voice_io.is_speaking, was_speaking)
            if action == "skip":
                _read_frame(np, stream)  # keep draining so the input buffer can't overflow
                continue
            # Only a genuinely-finished speak_response() opens the follow-up window — the
            # startup greeting and session narration (plain speak()) must NOT arm wake-free
            # capture, or every launch/narration would open ~10s of ambient listening (spec
            # §"Why NOT open-mic"). Polled (never while "skip" — see _followup_poll) so a
            # completion between polls (the speaking→idle edge missed entirely — e.g. TTS
            # finished during a blocking capture/transcribe) still opens follow-up.
            should_flush, should_open_window = _followup_poll(voice_io, action)
            if should_flush:
                for _ in range(flush_frames):
                    _read_frame(np, stream)
                wake.reset()  # clear accumulated TTS-audio context so it can't spuriously wake
                if should_open_window:
                    window.on_reply_finished()  # a response just finished → open follow-up
                continue

            frame = _read_frame(np, stream)
            # Muted: force the window closed and stop capturing (no STT on ambient
            # audio while the owner has muted). Honors ConversationWindow.reset's contract.
            if voice_io.is_paused:
                window.reset()
                continue
            scores = wake.predict(frame)  # always predict to keep the detector's buffer warm
            # predict() returns {model_name: score} — keyed by the model's NAME
            # (e.g. "hey_edith"), NOT the path/name we passed. Only one wake model
            # is loaded, so take the max score rather than guess the key.
            score = max(scores.values()) if isinstance(scores, dict) and scores else 0.0
            rms = _frame_rms(np, frame)
            if debug:
                # ~1 s heartbeat: mic level (rms) tells us if audio is arriving at
                # all; peak wake score tells us how close we are to the threshold.
                n_frames += 1
                peak = max(peak, float(score))
                if n_frames % 12 == 0:
                    print(f"[debug] mic_rms={rms:8.1f}  peak_wake_score={peak:.3f}"
                          f"  (wake {wake_threshold}, endpoint {endpoint_threshold})", flush=True)
                    peak = 0.0

            # Trigger: inside the follow-up window, speech ENERGY starts an utterance
            # (no wake word); otherwise the wake score must clear the threshold.
            if window.accepts_followup():
                triggered = rms >= endpoint_threshold
            else:
                triggered = float(score) >= wake_threshold
            if not triggered:
                preroll.append(frame)  # keep the quiet run-up available for the next trigger
                continue

            window.on_utterance()  # a real utterance is starting → keep the window hot
            # Capture until trailing silence (endpointed), transcribe, hand to VoiceIO.
            # The pre-roll supplies the frames BEFORE the trigger, where a word's opening
            # consonants live — they ramp up below the threshold and were being lost.
            pcm = _capture_endpointed(np, stream, endpointer, frame, tuple(preroll))
            preroll.clear()  # consumed — must not leak into the next utterance
            audio = pcm.astype(np.float32) / 32768.0
            segments, _info = stt.transcribe(audio, vad_filter=True)
            seg_list = list(segments)
            text = " ".join(s.text for s in seg_list).strip()
            if not text:
                continue
            confidence = _confidence(seg_list)
            # Bridge onto the event loop; _on_wake does barge-in + publish. Attach a
            # done-callback so an exception inside the coroutine is logged, not swallowed.
            fut = asyncio.run_coroutine_threadsafe(
                voice_io._on_wake(text, confidence), loop  # noqa: SLF001
            )
            fut.add_done_callback(_log_future_exc)


def _log_future_exc(fut: Any) -> None:
    """Log an exception raised inside a bridged ``_on_wake`` coroutine (never swallow)."""
    exc = fut.exception()
    if exc is not None:
        _log.error("voice: _on_wake failed", exc_info=exc)


def _frames_for_seconds(seconds: float, *, sample_rate: int, frame_samples: int) -> int:
    """Frame count covering at least *seconds* of audio (unit-tested; the loop is not).

    Rounds UP (``math.ceil``), never truncates: at 16 kHz/1280-sample frames, 0.3 s is
    3.75 frames — truncating to 3 would flush only ~240 ms, short of the configured
    ``EDITH_SPEAK_FLUSH_SECONDS`` and short-changing the TTS-tail drain it exists for.
    """
    return math.ceil(seconds * sample_rate / frame_samples)


def _gate_action(is_speaking: bool, was_speaking: bool) -> tuple[str, bool]:
    """Half-duplex mic gate as a pure state machine (unit-tested; the loop is not).

    Returns ``(action, next_was_speaking)`` where action is:
      - ``"skip"``    — EDITH is speaking → discard this frame, no detection.
      - ``"flush"``   — she just stopped → drain the TTS tail + reset the detector.
      - ``"process"`` — idle → run normal wake detection.
    """
    if is_speaking:
        return "skip", True
    if was_speaking:
        return "flush", False
    return "process", False


class FollowupSignalLike(Protocol):
    """The slice of VoiceIO that ``_followup_poll`` needs (spec 03 §Follow-up).

    Structural — a real ``VoiceIO`` satisfies this without inheriting from it (same
    pattern as ``edith.daemon.edithd.VoiceIOLike``).
    """

    @property
    def is_paused(self) -> bool: ...

    def consume_followup_ready(self) -> bool: ...


def _followup_poll(voice_io: FollowupSignalLike, gate_action: str) -> tuple[bool, bool]:
    """Consume the one-shot response-completion signal for one loop poll, UNLESS EDITH
    is still speaking (unit-tested; the loop is not).

    Must NOT consume — or even peek — the signal while ``gate_action`` is ``"skip"``:
    ``VoiceIO.is_speaking`` only marks a response ready once EVERY tracked utterance
    (including any overlapping narration or a second response) is idle, so while still
    speaking the signal — if any — is deliberately being retained, not lost. Reading it
    here anyway would just waste the one-shot read for no benefit. Once the gate is no
    longer "skip", consume it and combine with the gate action via ``_followup_transition``.
    """
    if gate_action == "skip":
        return False, False
    response_ready = voice_io.consume_followup_ready()
    return _followup_transition(gate_action, response_ready, voice_io.is_paused)


def _followup_transition(
    gate_action: str, response_ready: bool, muted: bool
) -> tuple[bool, bool]:
    """Pure follow-up decision for one loop poll (unit-tested; the loop is not).

    Combines the half-duplex gate's action with VoiceIO's one-shot response-completion
    signal (``consume_followup_ready``) so a response that finishes ENTIRELY between polls
    — the speaking→idle edge missed because the loop was blocked in a capture/transcribe —
    still opens follow-up, while a narration-only flush (no response armed) never does.

    Returns ``(should_flush, should_open_window)``:
      - ``should_flush``       — drain the TTS tail + reset the wake detector. True on a
        normal ``"flush"`` gate action OR whenever a response just completed, even if the
        gate missed the edge and reports ``"process"``.
      - ``should_open_window`` — open the follow-up ``ConversationWindow``. True only when
        a response completed AND the owner is not muted.
    """
    should_flush = gate_action == "flush" or response_ready
    should_open_window = response_ready and not muted
    return should_flush, should_open_window


def _read_frame(np: Any, stream: Any) -> Any:
    """Read one openWakeWord frame (int16 PCM) off the sounddevice stream."""
    data, _overflowed = stream.read(_FRAME_SAMPLES)
    return np.frombuffer(bytes(data), dtype=np.int16)


def _frame_rms(np: Any, frame: Any) -> float:
    """Root-mean-square energy of an int16 PCM frame (the endpointer's input)."""
    return float(np.sqrt(np.mean(np.square(frame.astype(np.float32)))))


def _capture_endpointed(
    np: Any,
    stream: Any,
    endpointer: Endpointer,
    first_frame: Any,
    preroll: Sequence[Any] = (),
) -> Any:
    """Read frames until the endpointer says the utterance ended (trailing silence / hard max).

    Starts from *preroll* + ``first_frame``, then feeds each frame's RMS to the pure
    ``Endpointer``. Owner-smoke only; the end-decision logic is unit-tested in
    test_voice_endpointing.py.

    **Why the pre-roll.** ``first_frame`` is the frame that *crossed the trigger*, not the
    frame speech began on. A word ramps up over 100-300 ms, so its opening consonants sit
    below the threshold and were simply never in the buffer — the owner reported EDITH
    "will not pick up the first two parts of my sentence", and lowering the threshold
    cannot fix it because the audio was already gone. The loop keeps the last few frames
    in a ring buffer and hands them over here, so capture starts *before* the trigger.
    """
    endpointer.reset()
    chunks = [*preroll, first_frame]
    for frame in chunks:
        # Leading sub-threshold frames do not count toward trailing silence (Endpointer
        # only starts that counter after speech), so feeding the pre-roll is safe.
        if endpointer.feed(_frame_rms(np, frame)):
            return np.concatenate(chunks)
    while True:
        frame = _read_frame(np, stream)
        chunks.append(frame)
        if endpointer.feed(_frame_rms(np, frame)):
            return np.concatenate(chunks)


def _confidence(segments: list[Any]) -> float:
    """Rough utterance confidence from mean segment avg_logprob → (0, 1]."""
    logprobs = [getattr(s, "avg_logprob", 0.0) for s in segments]
    if not logprobs:
        return 0.0
    return round(math.exp(sum(logprobs) / len(logprobs)), 3)
