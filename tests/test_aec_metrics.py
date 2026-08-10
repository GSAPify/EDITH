"""Tests for the AEC bench metric math (edith/voice/aec_bench/metrics.py).

Every expected value here is known *by construction* — a scaled copy of a signal has an
exactly known power ratio, and a zero-prepended copy has an exactly known delay. Nothing is
calibrated against a recording, so these tests stay true regardless of what hardware the
bench eventually runs on.

The load-bearing test in this file is
``test_a_half_duplex_gate_scores_infinite_erle_and_total_double_talk_loss``: it is what
stops the metric set from being gamed by suppress-everything.
"""

from __future__ import annotations

import numpy as np
import pytest

from edith.voice.aec_bench.metrics import (
    added_latency_ms,
    double_talk_attenuation_db,
    erle_db,
)

SAMPLE_RATE = 16000


def noise(n: int = 16000, seed: int = 0, scale: float = 1000.0) -> np.ndarray:
    """A deterministic int16 noise burst standing in for captured audio."""
    rng = np.random.default_rng(seed)
    return (rng.standard_normal(n) * scale).astype(np.int16)


# ---------------------------------------------------------------------------
# erle_db — higher is better, 0 dB means nothing was cancelled
# ---------------------------------------------------------------------------


def test_erle_db_of_a_tenth_amplitude_residual_is_20_db():
    mic = noise().astype(np.float64)
    assert erle_db(mic, mic / 10) == pytest.approx(20.0)


def test_erle_db_of_an_uncancelled_signal_is_zero():
    mic = noise()
    assert erle_db(mic, mic) == pytest.approx(0.0)


def test_erle_db_is_infinite_when_the_residual_is_silent():
    mic = noise()
    assert erle_db(mic, np.zeros(mic.size, dtype=np.int16)) == float("inf")


def test_erle_db_does_not_overflow_on_loud_int16_input():
    """int16 squared overflows; the metric must promote to float64 before squaring."""
    mic = np.full(1024, 30000, dtype=np.int16)
    assert erle_db(mic, mic // 10) == pytest.approx(20.0, abs=0.01)


def test_erle_db_compares_over_the_shorter_length():
    mic = noise(16000)
    assert erle_db(mic, (mic[:15000].astype(np.float64) / 10)) == pytest.approx(20.0)


# ---------------------------------------------------------------------------
# double_talk_attenuation_db — 0 dB is perfect, negative means the user was eaten
# ---------------------------------------------------------------------------


def test_double_talk_attenuation_is_zero_when_the_user_passes_through_untouched():
    near = noise(seed=1)
    assert double_talk_attenuation_db(near, near) == pytest.approx(0.0)


def test_double_talk_attenuation_of_a_hundredth_amplitude_is_minus_40_db():
    near = noise(seed=1).astype(np.float64)
    assert double_talk_attenuation_db(near, near / 100) == pytest.approx(-40.0)


def test_double_talk_attenuation_rejects_a_silent_near_end_reference():
    """A ratio against an all-zero reference is meaningless, not merely infinite."""
    silence = np.zeros(16000, dtype=np.int16)
    with pytest.raises(ValueError):
        double_talk_attenuation_db(silence, noise(seed=2))


def test_double_talk_attenuation_compares_over_the_shorter_length():
    near = noise(16000, seed=1)
    assert double_talk_attenuation_db(near, near[:15500]) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# added_latency_ms — sign convention is load-bearing
# ---------------------------------------------------------------------------


def delay_by(signal: np.ndarray, samples: int) -> np.ndarray:
    """Return ``signal`` delayed by ``samples``, same length, zero-filled at the front."""
    return np.concatenate([np.zeros(samples, dtype=signal.dtype), signal[:-samples]])


def test_added_latency_recovers_a_known_delay():
    reference = noise(seed=3)
    captured = delay_by(reference, 160)  # 10 ms at 16 kHz
    assert added_latency_ms(reference, captured, SAMPLE_RATE) == pytest.approx(
        10.0, abs=1000.0 / SAMPLE_RATE
    )


def test_added_latency_is_zero_for_an_aligned_capture():
    reference = noise(seed=3)
    assert added_latency_ms(reference, reference, SAMPLE_RATE) == pytest.approx(0.0)


def test_added_latency_is_negative_when_the_capture_leads_the_reference():
    """Guards the sign convention: a symmetric abs() implementation would pass the
    positive-delay test alone, and then report a lead as a lag on real recordings."""
    reference = noise(seed=3)
    captured = delay_by(reference, 160)
    assert added_latency_ms(captured, reference, SAMPLE_RATE) == pytest.approx(
        -10.0, abs=1000.0 / SAMPLE_RATE
    )


def test_added_latency_honours_the_sample_rate():
    reference = noise(seed=3)
    captured = delay_by(reference, 160)
    assert added_latency_ms(reference, captured, 8000) == pytest.approx(20.0, abs=0.25)


def test_added_latency_tolerates_a_length_mismatch():
    reference = noise(seed=3)
    captured = delay_by(reference, 160)[:-37]
    assert added_latency_ms(reference, captured, SAMPLE_RATE) == pytest.approx(
        10.0, abs=1000.0 / SAMPLE_RATE
    )


# ---------------------------------------------------------------------------
# The reason both dB metrics exist
# ---------------------------------------------------------------------------


def test_a_half_duplex_gate_scores_infinite_erle_and_total_double_talk_loss():
    """The metric set must be un-gameable by suppress-everything.

    EDITH's current gate zeroes the mic while she speaks. It must look perfect on ERLE and be
    disqualified on double-talk. If it ever passes BOTH, the metrics are wrong and every number
    measured with them is worthless.
    """
    rng = np.random.default_rng(0)
    mic = (rng.standard_normal(16000) * 1000).astype(np.int16)
    near_end = (rng.standard_normal(16000) * 1000).astype(np.int16)
    gate_output = np.zeros(16000, dtype=np.int16)

    assert erle_db(mic, gate_output) == float("inf")
    assert double_talk_attenuation_db(near_end, gate_output) == float("-inf")
