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
debug on first live use — this is the owner live-smoke surface. Known v1
simplifications, documented inline: fixed-window utterance capture (not energy
VAD), and barge-in fires when ``_on_wake`` runs (after capture) rather than at
the instant of wake-word detection.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
from typing import Any

from edith.bus import EventBus
from edith.voice.adapters import select_adapter
from edith.voice.io import VoiceIO
from edith.voice.tts import TTSAdapter

_log = logging.getLogger(__name__)

# 16 kHz mono is what both openWakeWord and faster-whisper expect. 1280 samples
# = 80 ms, openWakeWord's native frame size.
_SAMPLE_RATE = 16000
_FRAME_SAMPLES = 1280
_WAKE_MODEL = "hey_jarvis"  # openWakeWord bundles hey_jarvis_v0.1.onnx
_WAKE_THRESHOLD = 0.5
_UTTERANCE_SECONDS = 5.0  # v1: fixed capture window after wake (VAD is a follow-up)


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
    utterance_seconds: float = _UTTERANCE_SECONDS,
) -> None:
    """Always-listening loop: mic → wake → STT → ``voice_io`` publish.

    Runs the blocking audio loop in a worker thread and bridges each recognised
    utterance back onto the event loop via ``run_coroutine_threadsafe``. Blocks
    until cancelled. NOT headless-verified.
    """
    loop = asyncio.get_running_loop()
    await asyncio.to_thread(
        _blocking_listen, voice_io, loop, wake_model, wake_threshold, stt_model, utterance_seconds
    )


def _blocking_listen(
    voice_io: VoiceIO,
    loop: asyncio.AbstractEventLoop,
    wake_model: str,
    wake_threshold: float,
    stt_model: str,
    utterance_seconds: float,
) -> None:
    """The blocking mic loop — runs in a worker thread (heavy imports here)."""
    import numpy as np
    import sounddevice as sd
    from faster_whisper import WhisperModel
    from openwakeword.model import Model

    wake = Model(wakeword_models=[wake_model])
    stt = WhisperModel(stt_model, device="cpu", compute_type="int8")
    frames_per_utterance = int(_SAMPLE_RATE * utterance_seconds / _FRAME_SAMPLES)
    _log.info("VoiceIO live loop up: wake=%s stt=%s", wake_model, stt_model)

    debug = os.environ.get("EDITH_VOICE_DEBUG") == "1"
    # Half-duplex gate: while EDITH speaks, discard mic frames; when she stops,
    # flush the residual TTS tail and RESET the detector (the primary defense —
    # a sub-second leaked fragment can't complete a ~1.5 s wake phrase after a
    # reset). Flush length is env-tunable for the live retest (no recompile).
    flush_seconds = float(os.environ.get("EDITH_SPEAK_FLUSH_SECONDS", "0.8"))
    flush_frames = int(_SAMPLE_RATE * flush_seconds / _FRAME_SAMPLES)
    was_speaking = False
    n_frames, peak = 0, 0.0
    with sd.RawInputStream(
        samplerate=_SAMPLE_RATE, channels=1, dtype="int16", blocksize=_FRAME_SAMPLES
    ) as stream:
        while True:
            action, was_speaking = _gate_action(voice_io.is_speaking, was_speaking)
            if action == "skip":
                _read_frame(np, stream)  # keep draining so the input buffer can't overflow
                continue
            if action == "flush":
                for _ in range(flush_frames):
                    _read_frame(np, stream)
                wake.reset()  # clear accumulated TTS-audio context so it can't spuriously wake
                continue

            frame = _read_frame(np, stream)
            scores = wake.predict(frame)
            # predict() returns {model_name: score} — keyed by the model's NAME
            # (e.g. "hey_edith"), NOT the path/name we passed. Only one wake model
            # is loaded, so take the max score rather than guess the key.
            score = max(scores.values()) if isinstance(scores, dict) and scores else 0.0
            if debug:
                # ~1 s heartbeat: mic level (rms) tells us if audio is arriving at
                # all; peak wake score tells us how close we are to the threshold.
                n_frames += 1
                peak = max(peak, float(score))
                if n_frames % 12 == 0:
                    rms = float(np.sqrt(np.mean(np.square(frame.astype(np.float32)))))
                    print(f"[debug] mic_rms={rms:8.1f}  peak_wake_score={peak:.3f}"
                          f"  (threshold {wake_threshold})", flush=True)
                    peak = 0.0
            if float(score) < wake_threshold:
                continue

            # Wake detected — capture a fixed window, transcribe, hand to VoiceIO.
            pcm = _capture_utterance(np, stream, frames_per_utterance)
            audio = pcm.astype(np.float32) / 32768.0
            segments, _info = stt.transcribe(audio, vad_filter=True)
            seg_list = list(segments)
            text = " ".join(s.text for s in seg_list).strip()
            if not text:
                continue
            confidence = _confidence(seg_list)
            # Bridge onto the event loop; _on_wake does barge-in + publish.
            asyncio.run_coroutine_threadsafe(voice_io._on_wake(text, confidence), loop)  # noqa: SLF001


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


def _read_frame(np: Any, stream: Any) -> Any:
    """Read one openWakeWord frame (int16 PCM) off the sounddevice stream."""
    data, _overflowed = stream.read(_FRAME_SAMPLES)
    return np.frombuffer(bytes(data), dtype=np.int16)


def _capture_utterance(np: Any, stream: Any, frames: int) -> Any:
    """Read a fixed-length int16 PCM window (v1: no energy VAD, see module note)."""
    chunks = [_read_frame(np, stream) for _ in range(max(frames, 1))]
    return np.concatenate(chunks)


def _confidence(segments: list[Any]) -> float:
    """Rough utterance confidence from mean segment avg_logprob → (0, 1]."""
    logprobs = [getattr(s, "avg_logprob", 0.0) for s in segments]
    if not logprobs:
        return 0.0
    return round(math.exp(sum(logprobs) / len(logprobs)), 3)
