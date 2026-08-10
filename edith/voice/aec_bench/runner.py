"""The bench runner — drives a DuplexAudio through one measurement run.

Play a stimulus, capture the cancelled mic, archive all three signals, compute the metrics,
write the result row.

Archiving mic_raw / reference / cancelled is what makes a bad result debuggable -- without
mic_raw you cannot distinguish "the canceller failed" from "the speaker was muted".
"""

from __future__ import annotations

import json
import wave
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from edith.voice.aec_bench.metrics import added_latency_ms, double_talk_attenuation_db, erle_db
from edith.voice.duplex import FRAME_SAMPLES, SAMPLE_RATE

# A run must be long enough to expose clock drift between two independent audio streams.
# A short run hides exactly the failure mode that matters for an assistant that monologues.
MIN_RUN_SECONDS = 30.0

# If the mic hears nothing while the stimulus is playing, the speaker is muted or the volume
# is at zero. That would score superb ERLE and produce entirely the wrong conclusion -- the
# ERLE trap wearing a different hat. Fail loudly instead.
SILENT_MIC_RMS_FLOOR = 20.0


@dataclass(frozen=True)
class RunResult:
    """One bench run. Mirrors the aec_run table one-for-one."""

    started_at: str
    backend: str
    hardware: str
    stimulus: str
    erle_db: float | None
    double_talk_attenuation_db: float | None
    added_latency_ms: float | None
    wake_detect_rate: float | None
    wav_dir: str
    notes: str


class SilentMicError(RuntimeError):
    """The mic heard no echo during playback — speaker muted, or volume at zero."""


def write_wav(path: Path, samples: np.ndarray, sample_rate: int = SAMPLE_RATE) -> None:
    """Write mono int16 PCM. Archived so a bad run can be re-analysed without the hardware."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(samples.astype(np.int16).tobytes())


def capture_during_playback(
    duplex: Any, stimulus: np.ndarray, frame_samples: int = FRAME_SAMPLES
) -> np.ndarray:
    """Play *stimulus* while draining cancelled frames; return the captured signal.

    Playback is chunked frame-by-frame rather than written in one shot so capture and
    playback advance together — writing it all up front would let the whole stimulus drain
    before the first frame is read, and the "cancelled" capture would contain no echo at all.
    """
    captured: list[np.ndarray] = []
    frames = duplex.frames()
    for start in range(0, len(stimulus) - frame_samples + 1, frame_samples):
        duplex.play(stimulus[start : start + frame_samples].astype(np.int16).tobytes())
        try:
            captured.append(next(frames))
        except StopIteration:
            break
    return np.concatenate(captured) if captured else np.zeros(0, dtype=np.int16)


def rms(samples: np.ndarray) -> float:
    if samples.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(samples.astype(np.float64)))))


def run_once(
    duplex: Any,
    stimulus: np.ndarray,
    *,
    backend: str,
    hardware: str,
    stimulus_name: str,
    out_dir: Path,
    near_end_alone: np.ndarray | None = None,
    notes: str = "",
) -> RunResult:
    """One measurement run against one backend.

    *near_end_alone* is the double-talk reference: the same near-end signal recorded WITHOUT
    playback. Omitted for a single-talk (ERLE-only) run.
    """
    started = datetime.now(UTC)
    stamp = started.strftime("%Y%m%dT%H%M%SZ")
    wav_dir = out_dir / f"{stamp}-{backend}-{hardware}"

    captured = capture_during_playback(duplex, stimulus)
    if rms(captured) < SILENT_MIC_RMS_FLOOR:
        raise SilentMicError(
            f"mic RMS {rms(captured):.1f} is below the floor {SILENT_MIC_RMS_FLOOR} during "
            "playback — no echo detected. Is the speaker on and unmuted?"
        )

    write_wav(wav_dir / "reference.wav", stimulus)
    write_wav(wav_dir / "cancelled.wav", captured)

    result = RunResult(
        started_at=started.isoformat(),
        backend=backend,
        hardware=hardware,
        stimulus=stimulus_name,
        erle_db=erle_db(stimulus, captured),
        double_talk_attenuation_db=(
            double_talk_attenuation_db(near_end_alone, captured)
            if near_end_alone is not None
            else None
        ),
        added_latency_ms=added_latency_ms(stimulus, captured, SAMPLE_RATE),
        wake_detect_rate=None,  # filled by the separate end-to-end confirmation run
        wav_dir=str(wav_dir),
        notes=notes,
    )
    append_result(out_dir / "results.jsonl", result)
    return result


def append_result(path: Path, result: RunResult) -> None:
    """results.jsonl is the source of truth; Postgres is only a view over it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(asdict(result)) + "\n")


def render(result: RunResult) -> str:
    """Human-readable report. Sign conventions are spelled out because they are opposite:
    ERLE higher-is-better, double-talk attenuation zero-is-better."""
    lines = [
        "",
        f"AEC run — {result.backend} on {result.hardware}",
        "=" * 60,
        f"  ERLE                     {result.erle_db:8.1f} dB   (higher is better)",
    ]
    if result.double_talk_attenuation_db is not None:
        lines.append(
            f"  Double-talk attenuation  {result.double_talk_attenuation_db:8.1f} dB   "
            "(0 is perfect)"
        )
    lines += [
        f"  Added latency            {result.added_latency_ms:8.1f} ms",
        "=" * 60,
        f"  audio archived to {result.wav_dir}",
        "",
    ]
    return "\n".join(lines)
