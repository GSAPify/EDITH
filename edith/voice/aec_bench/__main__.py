"""``python -m edith.voice.aec_bench`` — run one echo-cancellation measurement.

  python -m edith.voice.aec_bench --backend vpio --hardware macbook --seconds 30

Plays a deterministic stimulus through the backend's speaker while capturing its cancelled
mic, archives all three signals, computes the metrics and appends a row to
``results.jsonl``.

**ERLE and latency need nothing but this laptop.** Double-talk is the exception: it measures
how much of the NEAR-END survives, and the near-end must be a signal the canceller does not
have in its reference. Anything played through the laptop's own speaker IS the reference, so
using it would measure "the echo was cancelled" and report a catastrophic score for a
canceller working perfectly. Hence ``--near-end``: a WAV recorded from a second source (a
phone at a fixed position) played back during the run. Omit it and double-talk is reported as
null rather than fabricated.

Owner live-smoke: needs a real microphone and speaker.
"""

from __future__ import annotations

import argparse
import sys
import wave
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

from edith.voice.aec_bench.runner import (
    MIN_RUN_SECONDS,
    SilentMicError,
    render,
    run_erle_measurement,
)
from edith.voice.aec_bench.stimulus import chirp, speech_shaped_noise
from edith.voice.duplex import SAMPLE_RATE, DuplexUnavailable

DEFAULT_OUT_DIR = "~/.edith/aec_bench"


def build_backend(name: str, enable_aec: bool = True) -> object:
    """Construct a DuplexAudio backend by name.

    *enable_aec* False builds the same audio graph with cancellation off — the first half of
    the ERLE measurement, not a debug switch. Imports are local so a missing optional
    dependency for one backend does not stop the other from running.
    """
    if name == "vpio":
        from edith.voice.duplex.vpio import VpioDuplex

        # The bench stimulus is generated at SAMPLE_RATE (16 kHz), NOT at the ElevenLabs
        # 24 kHz the backend defaults to. Leaving the default resamples 16 kHz audio as if it
        # were 24 kHz: it plays ~1.5x too fast, which corrupts the echo AND destroys the
        # cross-correlation the latency measurement depends on. Measured as ERLE 2.1 dB /
        # latency 2382 ms before this was passed explicitly.
        return VpioDuplex(enable_aec=enable_aec, pcm_sample_rate=SAMPLE_RATE)
    if name == "speex":
        raise DuplexUnavailable(
            "SpeexDuplex is not implemented yet — only the SpeexEchoCanceller core exists. "
            "Use --backend vpio."
        )
    raise DuplexUnavailable(f"unknown backend {name!r}")


def load_wav_16k(path: Path) -> NDArray[np.int16]:
    """Read a mono 16-bit 16 kHz WAV, refusing anything else.

    A silently-resampled or stereo near-end reference would corrupt the double-talk number
    without any visible symptom, so this is strict rather than accommodating.
    """
    with wave.open(str(path), "rb") as handle:
        if handle.getnchannels() != 1:
            raise ValueError(f"{path}: need mono, got {handle.getnchannels()} channels")
        if handle.getsampwidth() != 2:
            raise ValueError(f"{path}: need 16-bit samples")
        if handle.getframerate() != SAMPLE_RATE:
            raise ValueError(
                f"{path}: need {SAMPLE_RATE} Hz, got {handle.getframerate()} Hz"
            )
        return np.frombuffer(handle.readframes(handle.getnframes()), dtype=np.int16)


def build_stimulus(name: str, seconds: float) -> NDArray[np.int16]:
    if name == "chirp":
        return chirp(seconds)
    return speech_shaped_noise(seconds)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="edith.voice.aec_bench", description="Measure one echo-cancellation backend"
    )
    parser.add_argument("--backend", required=True, choices=["vpio", "speex"])
    parser.add_argument(
        "--hardware",
        required=True,
        help="which rig this run used — 'macbook', 'airpods', 'external'. Recorded on the "
        "row so results can be sliced by setup; the numbers are not comparable across them.",
    )
    parser.add_argument("--seconds", type=float, default=MIN_RUN_SECONDS)
    parser.add_argument("--stimulus", default="noise", choices=["noise", "chirp"])
    parser.add_argument(
        "--near-end",
        type=Path,
        default=None,
        help="mono 16 kHz WAV of the near-end signal recorded ALONE. Required for a "
        "double-talk number; play the same file from a second device during the run.",
    )
    parser.add_argument("--out-dir", default=DEFAULT_OUT_DIR)
    parser.add_argument("--notes", default="", help="rig details worth recording: phone "
                        "position, speaker volume, room")
    args = parser.parse_args(argv)

    if args.seconds < MIN_RUN_SECONDS:
        print(
            f"[aec-bench] WARNING: {args.seconds:.0f}s is below the {MIN_RUN_SECONDS:.0f}s "
            "minimum. Short runs hide the clock drift between independent audio streams — "
            "the exact failure that matters for an assistant that monologues.",
            file=sys.stderr,
        )

    near_end = None
    if args.near_end is not None:
        try:
            near_end = load_wav_16k(args.near_end)
        except (OSError, ValueError, wave.Error) as exc:
            print(f"[aec-bench] cannot read --near-end: {exc}", file=sys.stderr)
            return 2
    else:
        print(
            "[aec-bench] no --near-end given: reporting ERLE and latency only. Double-talk "
            "needs a near-end signal from a second device (the laptop speaker is already the "
            "canceller's reference, so it cannot supply one).",
            file=sys.stderr,
        )

    stimulus = build_stimulus(args.stimulus, args.seconds)
    print(
        f"[aec-bench] {args.backend} on {args.hardware}: TWO passes of {args.seconds:.0f}s "
        f"{args.stimulus} — cancellation off, then on. ERLE is the ratio between them, so both "
        "must traverse the same acoustic path. Keep the room quiet and do not move the laptop "
        "between passes.",
        file=sys.stderr,
    )
    try:
        result = run_erle_measurement(
            lambda aec_on: build_backend(args.backend, aec_on),
            stimulus,
            backend=args.backend,
            hardware=args.hardware,
            stimulus_name=args.stimulus,
            out_dir=Path(args.out_dir).expanduser(),
            near_end_alone=near_end,
            notes=args.notes,
        )
    except DuplexUnavailable as exc:
        print(f"[aec-bench] backend {args.backend!r} unavailable: {exc}", file=sys.stderr)
        return 1
    except SilentMicError as exc:
        print(f"[aec-bench] {exc}", file=sys.stderr)
        return 3

    print(render(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
