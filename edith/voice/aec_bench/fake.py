"""A DuplexAudio that replays an archived WAV as if it were live cancelled capture.

This is what lets the bench run with NO hardware: ``runner.py`` and the metric math can be
exercised end-to-end against a recording, and ``play()`` records rather than emits so a test
can assert the runner actually sent the stimulus.
"""

from __future__ import annotations

import wave
from collections.abc import Iterator
from pathlib import Path

import numpy as np

from edith.voice.duplex import FRAME_SAMPLES, SAMPLE_RATE, DuplexUnavailable


class FakeDuplex:
    """Replay ``mic_wav`` as cancelled mic frames; record everything handed to ``play()``."""

    def __init__(self, mic_wav: str | Path) -> None:
        self.path = Path(mic_wav)
        self.played: list[bytes] = []
        self.closed = False
        with wave.open(str(self.path), "rb") as wav:
            rate = wav.getframerate()
            if rate != SAMPLE_RATE:
                # Resampling here would silently change the signal every metric is computed
                # from, so refuse the fixture instead and name both rates.
                raise ValueError(
                    f"{self.path} is {rate} Hz; the bench requires {SAMPLE_RATE} Hz"
                )
            pcm = wav.readframes(wav.getnframes())
        self.samples = np.frombuffer(pcm, dtype=np.int16)

    def frames(self) -> Iterator[np.ndarray]:
        """Yield consecutive int16 frames of exactly ``FRAME_SAMPLES``.

        A trailing partial frame is dropped, not zero-padded: padding would feed the metrics
        silence the canceller never saw and inflate ERLE.
        """
        if self.closed:
            raise DuplexUnavailable(f"{self.path} duplex is closed")
        whole = len(self.samples) // FRAME_SAMPLES
        return iter(
            self.samples[start : start + FRAME_SAMPLES]
            for start in range(0, whole * FRAME_SAMPLES, FRAME_SAMPLES)
        )

    def play(self, pcm: bytes) -> None:
        """Record the far-end audio. Nothing reaches a speaker."""
        self.played.append(pcm)

    def close(self) -> None:
        """Idempotent — the bench closes on every exit path, including the error ones."""
        self.closed = True
