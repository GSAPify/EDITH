"""The duplex-audio seam: cancelled mic frames in, playback audio out.

The seam sits HERE rather than at ``cancel(mic, ref) -> clean`` because the two candidate
backends do not have the same shape. WebRTC AEC3 is a pure DSP function and can run offline
on recorded audio. macOS Voice Processing I/O is an *I/O unit* that owns the live hardware on
both ends and cancels internally -- it has no "process this buffer" entry point at all. Only a
seam at this level can hold both.

``frames()`` yields exactly the framing ``edith/voice/live.py`` already consumes (16 kHz mono
int16, 1280 samples), so a winning backend is drop-in: ``_read_frame(np, stream)`` becomes
``next(duplex.frames())`` and wake detection, endpointing and the pre-roll ring are untouched.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, Protocol

SAMPLE_RATE = 16000
FRAME_SAMPLES = 1280


class DuplexUnavailable(RuntimeError):
    """A backend cannot initialise on this machine.

    Raised rather than returned so the bench records a failed run and continues to the other
    backend: one unavailable dependency must not cost the measurement that is still obtainable.
    """


class DuplexAudio(Protocol):
    """Full-duplex audio with echo cancellation applied to the capture side."""

    def frames(self) -> Iterator[Any]:
        """Yield cancelled mic frames: 16 kHz mono int16, ``FRAME_SAMPLES`` long."""
        ...

    def play(self, pcm: bytes) -> None:
        """Send TTS audio out. The backend decides how this reaches the canceller."""

    def close(self) -> None:
        """Release audio devices."""
