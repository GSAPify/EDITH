"""Deterministic stimulus for the AEC bench (plan 1, worker 3 lane).

Every number the bench reports is a ratio between what we played and what came back, so the
stimulus itself has to be reproducible to the byte. If ``speech_shaped_noise(seed=0)`` differed
between two runs, an ERLE delta between VPIO and AEC3 could just be a different noise draw —
the comparison would measure nothing. Hence the byte-identity test below is load-bearing, not
a nicety.
"""

from __future__ import annotations

import numpy as np
import pytest

from edith.voice.aec_bench.stimulus import chirp, speech_shaped_noise

SAMPLE_RATE = 16000
FULL_SCALE = 32767


def zero_crossings(signal: np.ndarray) -> int:
    """Count sign changes — a proxy for instantaneous frequency (crossings ≈ 2f per second)."""
    return int(np.count_nonzero(np.diff(np.signbit(signal))))


def test_chirp_length_and_dtype() -> None:
    assert chirp(1.0).shape == (16000,)
    assert chirp(0.5).shape == (8000,)
    assert chirp(2.0).shape == (32000,)
    assert chirp(1.0, sample_rate=8000).shape == (8000,)
    assert chirp(1.0).dtype == np.int16


def test_chirp_is_near_30_percent_of_full_scale_and_never_clips() -> None:
    peak = int(np.max(np.abs(chirp(1.0))))
    assert peak < FULL_SCALE
    assert 8000 <= peak <= 12000


def test_chirp_instantaneous_frequency_rises() -> None:
    """Zero-crossing density in the last tenth must far exceed the first tenth."""
    signal = chirp(1.0)
    tenth = len(signal) // 10
    first = zero_crossings(signal[:tenth])
    last = zero_crossings(signal[-tenth:])
    assert last > first * 3


def test_chirp_sweep_ends_near_f1_not_double_it() -> None:
    """Guards the classic sweep bug: multiplying by f(t) instead of integrating it.

    ``2*pi*(f0 + (f1-f0)*t/T)*t`` also makes density rise, but reaches 2*f1 — it would put
    energy above the 8 kHz Nyquist of a 16 kHz bench and alias into the measurement band.
    """
    signal = chirp(1.0, f0=200.0, f1=4000.0)
    tenth = len(signal) // 10
    seconds = tenth / SAMPLE_RATE
    hz_at_end = zero_crossings(signal[-tenth:]) / (2 * seconds)
    assert 3000.0 < hz_at_end < 5000.0


def test_chirp_has_no_rng_so_repeated_calls_are_identical() -> None:
    assert chirp(0.5).tobytes() == chirp(0.5).tobytes()


def test_speech_shaped_noise_length_and_dtype() -> None:
    assert speech_shaped_noise(1.0).shape == (16000,)
    assert speech_shaped_noise(0.5).shape == (8000,)
    assert speech_shaped_noise(1.0, sample_rate=8000).shape == (8000,)
    assert speech_shaped_noise(1.0).dtype == np.int16


def test_speech_shaped_noise_is_near_30_percent_of_full_scale_and_never_clips() -> None:
    peak = int(np.max(np.abs(speech_shaped_noise(1.0))))
    assert peak < FULL_SCALE
    assert 8000 <= peak <= 12000


def test_speech_shaped_noise_same_seed_is_byte_identical() -> None:
    assert speech_shaped_noise(1.0, seed=7).tobytes() == speech_shaped_noise(1.0, seed=7).tobytes()


def test_speech_shaped_noise_different_seeds_differ() -> None:
    assert not np.array_equal(
        speech_shaped_noise(1.0, seed=0), speech_shaped_noise(1.0, seed=1)
    )


def test_speech_shaped_noise_rolls_off_like_speech_not_flat_like_white_noise() -> None:
    """A flat spectrum would pass every other test here, so pin the tilt explicitly."""
    signal = speech_shaped_noise(2.0, seed=0).astype(np.float64)
    power = np.abs(np.fft.rfft(signal)) ** 2
    freqs = np.fft.rfftfreq(len(signal), 1.0 / SAMPLE_RATE)

    low = power[(freqs >= 100.0) & (freqs < 1000.0)].sum()
    high = power[(freqs >= 4000.0) & (freqs <= 8000.0)].sum()
    assert low > high * 4


def test_speech_shaped_noise_has_no_dc_offset() -> None:
    """A DC pedestal would show up as bogus residual power in every ERLE reading."""
    signal = speech_shaped_noise(1.0, seed=0).astype(np.float64)
    assert abs(signal.mean()) < 1.0


@pytest.mark.parametrize("duration_s", [0.0, -0.5, -1.0])
def test_non_positive_duration_raises(duration_s: float) -> None:
    with pytest.raises(ValueError):
        chirp(duration_s)
    with pytest.raises(ValueError):
        speech_shaped_noise(duration_s)
