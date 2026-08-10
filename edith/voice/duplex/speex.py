"""SpeexDSP echo canceller — the software arm of the backend comparison.

**This is not WebRTC AEC3.** The design called for AEC3 (the canceller lineage Zoom, Teams and
Meet ship), but ``webrtc-audio-processing==0.1.3`` — the only PyPI package that resolves — cannot
build on Apple Silicon: its setup.py passes ``-mfloat-abi=hard -mfpu=neon`` (32-bit ARM flags,
rejected by clang on arm64) together with ``-DWEBRTC_LINUX`` on macOS. There is no homebrew
formula for it either.

SpeexDSP is what is actually runnable here: ``libspeexdsp.dylib`` is already installed, and its
echo canceller is the MDF adaptive filter that PulseAudio used for years. It is genuinely older
and weaker than AEC3, so read a Speex result accordingly — **if Speex loses to VPIO that does not
prove a modern software canceller would lose.** It rules out the software arm we can run today,
nothing more. A true AEC3 comparison needs the broken build vendored and patched.

Architecturally it is still the right shape for the comparison: unlike VPIO, we own the reference
plumbing and the delay handling, which is exactly where software cancellers are hard.
"""

from __future__ import annotations

import ctypes
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

from edith.voice.duplex import DuplexUnavailable

# Homebrew's install location on Apple Silicon. Checked before use so a missing library is a
# reported DuplexUnavailable rather than an OSError from deep inside ctypes.
LIBRARY_PATHS = (
    "/opt/homebrew/lib/libspeexdsp.dylib",
    "/usr/local/lib/libspeexdsp.dylib",
)

# Speex wants a frame size that divides the work evenly and a filter tail covering the real
# echo path. 256 samples = 16 ms at 16 kHz; a 4096-sample tail = 256 ms, comfortably longer
# than a laptop's speaker->air->mic delay.
DEFAULT_FRAME_SIZE = 256
DEFAULT_FILTER_LENGTH = 4096


def load_library() -> ctypes.CDLL:
    """Load libspeexdsp, or explain why the software backend is unavailable."""
    for path in LIBRARY_PATHS:
        if Path(path).exists():
            return ctypes.CDLL(path)
    raise DuplexUnavailable(
        "libspeexdsp not found (looked in "
        + ", ".join(LIBRARY_PATHS)
        + "). Install with: brew install speexdsp"
    )


class SpeexEchoCanceller:
    """A thin ctypes wrapper over SpeexDSP's MDF echo canceller.

    Deliberately separate from the duplex backend so the cancellation itself can be tested
    headlessly — the library is a plain C dependency, so a synthetic echo can be cancelled in
    a unit test with no audio hardware at all.
    """

    def __init__(
        self,
        frame_size: int = DEFAULT_FRAME_SIZE,
        filter_length: int = DEFAULT_FILTER_LENGTH,
        sample_rate: int = 16000,
    ) -> None:
        self._lib = load_library()
        self._lib.speex_echo_state_init.restype = ctypes.c_void_p
        self._lib.speex_echo_state_init.argtypes = [ctypes.c_int, ctypes.c_int]
        self._frame_size = frame_size

        state = self._lib.speex_echo_state_init(frame_size, filter_length)
        if not state:
            raise DuplexUnavailable("speex_echo_state_init returned NULL")
        self._state = ctypes.c_void_p(state)

        # SPEEX_ECHO_SET_SAMPLING_RATE == 24. Without it Speex assumes 8 kHz and its adaptation
        # rate is wrong for our 16 kHz frames.
        rate = ctypes.c_int(sample_rate)
        self._lib.speex_echo_ctl(self._state, 24, ctypes.byref(rate))

    @property
    def frame_size(self) -> int:
        return self._frame_size

    def cancel(self, mic: NDArray[np.int16], reference: NDArray[np.int16]) -> NDArray[np.int16]:
        """Remove *reference* (what we played) from *mic* (what we heard).

        Both must be exactly ``frame_size`` samples. Speex is a per-frame adaptive filter: it
        converges over successive calls, so a single frame in isolation is barely cancelled at
        all — that is the algorithm, not a bug.
        """
        if len(mic) != self._frame_size or len(reference) != self._frame_size:
            raise ValueError(
                f"both frames must be exactly {self._frame_size} samples, "
                f"got mic={len(mic)} reference={len(reference)}"
            )
        out = np.zeros(self._frame_size, dtype=np.int16)
        self._lib.speex_echo_cancellation(
            self._state,
            mic.astype(np.int16).ctypes.data_as(ctypes.POINTER(ctypes.c_short)),
            reference.astype(np.int16).ctypes.data_as(ctypes.POINTER(ctypes.c_short)),
            out.ctypes.data_as(ctypes.POINTER(ctypes.c_short)),
        )
        return out

    def close(self) -> None:
        """Free the echo state. Idempotent."""
        if getattr(self, "_state", None) is not None:
            self._lib.speex_echo_state_destroy(self._state)
            self._state = None  # type: ignore[assignment]

    def __del__(self) -> None:  # pragma: no cover - GC timing
        try:
            self.close()
        except Exception:  # noqa: BLE001 — never raise from a finaliser
            return
