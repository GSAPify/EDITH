"""SpeexDSP echo canceller wrapper.

libspeexdsp is a plain C dependency, so the cancellation itself is testable with **no audio
hardware** — a synthetic echo can be cancelled entirely in-process. That makes the software arm
of the backend comparison far cheaper to iterate on than VPIO, which needs a live device.
"""

from __future__ import annotations

import numpy as np
import pytest

from edith.voice.duplex import DuplexUnavailable
from edith.voice.duplex.speex import SpeexEchoCanceller

speex = pytest.importorskip("ctypes")  # ctypes is stdlib; the real gate is the dylib below


def canceller() -> SpeexEchoCanceller:
    try:
        return SpeexEchoCanceller()
    except DuplexUnavailable as exc:  # pragma: no cover - depends on the host
        pytest.skip(f"libspeexdsp unavailable: {exc}")


def test_frames_must_match_the_configured_frame_size() -> None:
    """Speex is a fixed-frame adaptive filter; a wrong length would read past the buffer."""
    aec = canceller()
    try:
        with pytest.raises(ValueError, match="exactly 256 samples"):
            aec.cancel(np.zeros(100, dtype=np.int16), np.zeros(256, dtype=np.int16))
    finally:
        aec.close()


def test_it_converges_and_removes_a_synthetic_echo() -> None:
    """Feed mic = a delayed, attenuated copy of the reference and watch it get cancelled.

    This is the whole point of the backend: the adaptive filter must LEARN the echo path over
    successive frames. A single frame is barely cancelled at all, so the test compares residual
    energy early in the run against residual energy after convergence — asserting on one frame
    would fail for reasons that are the algorithm working as designed.
    """
    aec = canceller()
    try:
        rng = np.random.default_rng(0)
        n = aec.frame_size
        early, late = [], []

        for i in range(200):
            reference = (rng.standard_normal(n) * 4000).astype(np.int16)
            # the "echo": the same signal at half amplitude, as if heard back off the speaker
            mic = (reference.astype(np.float64) * 0.5).astype(np.int16)
            out = aec.cancel(mic, reference)
            residual = float(np.sqrt(np.mean(out.astype(np.float64) ** 2)))
            if i < 10:
                early.append(residual)
            elif i >= 190:
                late.append(residual)

        assert np.mean(late) < np.mean(early), (
            f"the filter must converge: early residual {np.mean(early):.1f} "
            f"-> late {np.mean(late):.1f}"
        )
    finally:
        aec.close()


def test_silence_in_silence_out() -> None:
    aec = canceller()
    try:
        out = aec.cancel(np.zeros(256, dtype=np.int16), np.zeros(256, dtype=np.int16))
        assert np.all(out == 0)
    finally:
        aec.close()


def test_close_is_idempotent() -> None:
    aec = canceller()
    aec.close()
    aec.close()  # must not raise or double-free
