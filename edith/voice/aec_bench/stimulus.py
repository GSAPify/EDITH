"""Deterministic far-end stimulus for the echo-cancellation bench.

Two generators, both pure: no I/O, no globals, no hardware. ``chirp`` sweeps the band so a
single recording yields both ERLE across frequency and — because its autocorrelation has one
sharp peak — an unambiguous cross-correlation delay. ``speech_shaped_noise`` is the workload
stimulus: a canceller tuned on a pure tone can look far better than it will on TTS, so the
number that decides between backends is measured against something speech-like.

Amplitude is fixed at ~30% of full scale for both. Loud enough that the residual after
cancellation sits well above the int16 noise floor, quiet enough that neither the loudspeaker
nor the mic preamp clips — clipping is a nonlinearity, and a nonlinearity in the echo path is
exactly what a linear canceller cannot remove, so it would show up as a fake ERLE ceiling.

Sample rate defaults to 16000 Hz to match ``edith/voice/live.py`` and openWakeWord.
"""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray

SAMPLE_RATE = 16000

# Peak amplitude: ~30% of int16 full scale, leaving ~10 dB of headroom for the echo path.
PEAK_AMPLITUDE = 0.3 * 32767.0

# Long-term average speech spectrum, approximated: flat through the first formant region,
# then -9 dB/octave (amplitude ∝ f**-1.5), which is the standard LTASS tilt.
SPEECH_TILT_KNEE_HZ = 500.0
SPEECH_TILT_EXPONENT = -1.5

# Below this, roll off steeply. Real speech has almost nothing here, and rumble that a
# loudspeaker cannot reproduce would be pure reference-signal energy with no echo to match.
SPEECH_HIGHPASS_HZ = 100.0


def sample_count(duration_s: float, sample_rate: int) -> int:
    """Samples for a duration, rejecting non-positive durations before they reach numpy."""
    if duration_s <= 0:
        raise ValueError(f"duration_s must be positive, got {duration_s!r}")
    if sample_rate <= 0:
        raise ValueError(f"sample_rate must be positive, got {sample_rate!r}")
    return int(round(duration_s * sample_rate))


def to_int16(signal: NDArray[np.float64]) -> NDArray[np.int16]:
    """Scale to ``PEAK_AMPLITUDE`` and quantise, so no generator can clip."""
    peak = float(np.max(np.abs(signal))) if signal.size else 0.0
    if peak > 0.0:
        signal = signal * (PEAK_AMPLITUDE / peak)
    return np.round(signal).astype(np.int16)


def chirp(
    duration_s: float,
    sample_rate: int = SAMPLE_RATE,
    f0: float = 200.0,
    f1: float = 4000.0,
) -> NDArray[np.int16]:
    """Linear frequency sweep from ``f0`` to ``f1`` as int16. Used for ERLE and latency.

    Fully deterministic — no RNG — so two runs of the bench play byte-identical audio.

    The phase is the *integral* of the frequency ramp, not ``2*pi*f(t)*t``: the latter
    sweeps to ``2*f1`` and would alias off the 8 kHz Nyquist of a 16 kHz bench.
    """
    n = sample_count(duration_s, sample_rate)
    t = np.arange(n, dtype=np.float64) / sample_rate
    total_s = n / sample_rate
    phase = 2.0 * np.pi * (f0 * t + (f1 - f0) * t**2 / (2.0 * total_s))
    return to_int16(np.sin(phase))


def speech_shaped_noise(
    duration_s: float,
    sample_rate: int = SAMPLE_RATE,
    seed: int = 0,
) -> NDArray[np.int16]:
    """Noise filtered to a speech-like spectrum, as int16. The far-end bench stimulus.

    Closer to real TTS than a tone, so measured cancellation reflects what the canceller will
    face in production.

    Same ``seed`` gives byte-identical output: the whole backend comparison is a difference of
    ratios measured against this signal, so it must not vary between runs.
    """
    n = sample_count(duration_s, sample_rate)
    rng = np.random.default_rng(seed)
    spectrum = np.fft.rfft(rng.standard_normal(n))
    freqs = np.fft.rfftfreq(n, 1.0 / sample_rate)

    gain = np.ones_like(freqs)
    below = freqs < SPEECH_HIGHPASS_HZ
    gain[below] = (freqs[below] / SPEECH_HIGHPASS_HZ) ** 2  # also zeroes DC at f == 0
    above = freqs > SPEECH_TILT_KNEE_HZ
    gain[above] = (freqs[above] / SPEECH_TILT_KNEE_HZ) ** SPEECH_TILT_EXPONENT

    return to_int16(np.fft.irfft(spectrum * gain, n=n))
