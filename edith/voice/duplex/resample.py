"""Pure sample-rate and framing helpers for the duplex backends.

Split out of ``vpio.py`` so the arithmetic most likely to be subtly wrong — decimation,
re-blocking, int16/float conversion — is unit-testable without a microphone. Only the
CoreAudio wiring itself needs hardware.

macOS Voice Processing I/O imposes 48 kHz (measured on the owner's machine: the input node
reports 48000 Hz and delivers 4800-frame buffers). openWakeWord and ``edith/voice/live.py``
need 16 kHz in 1280-sample frames, so every captured buffer crosses a clean 3:1 decimation.
"""

from __future__ import annotations

from collections.abc import Iterator

import numpy as np
from numpy.typing import NDArray

VPIO_SAMPLE_RATE = 48000
TARGET_SAMPLE_RATE = 16000
DECIMATION = VPIO_SAMPLE_RATE // TARGET_SAMPLE_RATE  # exactly 3


# Anti-alias low-pass, designed once at import. 7 kHz cutoff leaves a guard band below the
# 8 kHz Nyquist of the decimated rate. A 3-tap moving average was tried first and measured
# only ~3x attenuation at 12 kHz — not enough to stop out-of-band energy folding into the
# speech band, which is why this is a windowed sinc instead.
_FILTER_TAPS = 31
_FILTER_CUTOFF_HZ = 7000.0


def lowpass_kernel(
    num_taps: int = _FILTER_TAPS,
    cutoff_hz: float = _FILTER_CUTOFF_HZ,
    sample_rate: int = VPIO_SAMPLE_RATE,
) -> NDArray[np.float64]:
    """Windowed-sinc low-pass, normalised to unity DC gain."""
    n = np.arange(num_taps) - (num_taps - 1) / 2.0
    fc = cutoff_hz / sample_rate
    kernel = 2.0 * fc * np.sinc(2.0 * fc * n) * np.hamming(num_taps)
    return kernel / kernel.sum()


_ANTI_ALIAS = lowpass_kernel()


def decimate_to_16k(samples: NDArray[np.float64]) -> NDArray[np.float64]:
    """48 kHz -> 16 kHz by 3:1 decimation, low-passed first.

    Dropping every third sample without filtering folds everything above 8 kHz back down into
    the speech band as alias noise, which would corrupt both the wake detector and any
    measurement taken through this path.
    """
    if samples.size == 0:
        return samples.astype(np.float64)
    smoothed = np.convolve(samples.astype(np.float64), _ANTI_ALIAS, mode="same")
    return smoothed[::DECIMATION]


def float_to_int16(samples: NDArray[np.float64]) -> NDArray[np.int16]:
    """Clamp to [-1, 1] and scale to int16.

    Clamping rather than letting numpy wrap: a value above 1.0 would wrap to a large
    NEGATIVE int16, turning a loud moment into a discontinuity that reads as a click to the
    wake detector.
    """
    clipped = np.clip(samples, -1.0, 1.0)
    return (clipped * 32767.0).astype(np.int16)


def int16_to_float(samples: NDArray[np.int16]) -> NDArray[np.float64]:
    """int16 PCM -> float in [-1, 1), the format CoreAudio buffers want."""
    return samples.astype(np.float64) / 32768.0


def reblock(
    buffer: NDArray[np.int16], frame_samples: int
) -> tuple[list[NDArray[np.int16]], NDArray[np.int16]]:
    """Split *buffer* into whole frames; return them plus the leftover tail.

    VPIO hands over 4800-sample buffers while the wake detector wants 1280, so the two never
    line up. The remainder MUST be carried into the next call rather than dropped or padded:
    dropping loses ~4 ms of speech per buffer, and zero-padding injects fake silence that the
    endpointer would read as a pause.
    """
    frames = [
        buffer[i : i + frame_samples]
        for i in range(0, len(buffer) - frame_samples + 1, frame_samples)
    ]
    consumed = len(frames) * frame_samples
    return frames, buffer[consumed:]


def frames_of(samples: NDArray[np.int16], frame_samples: int) -> Iterator[NDArray[np.int16]]:
    """Yield whole frames, dropping any trailing partial frame."""
    for i in range(0, len(samples) - frame_samples + 1, frame_samples):
        yield samples[i : i + frame_samples]
