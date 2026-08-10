"""Metric math for the echo-cancellation backend comparison.

Three pure functions. No I/O, no hardware, no globals — every number these produce is
reproducible from the arrays passed in.

**Why two dB metrics, and why neither can be dropped.** EDITH's current half-duplex gate
discards every mic frame while she speaks. That scores *infinite* ERLE (100% of the echo
removed) while having zero duplex (100% of the user removed too). Ranking on ERLE alone would
therefore crown the very code this bench exists to replace. Pairing it with double-talk
attenuation is what makes the set un-gameable: a backend has to score high ERLE **and**
near-zero attenuation at the same time, and suppress-everything can only ever win one of them.

Everything is computed in float64. int16 arrays overflow when squared — a 30000-count sample
squared is ~9e8, well past int16 — so promoting first is correctness, not tidiness.
"""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray

SAMPLE_RATE = 16000


def common_length(a: NDArray, b: NDArray) -> tuple[NDArray, NDArray]:
    """Truncate both signals to their shared length, as float64.

    Archived recordings routinely differ by a frame or two, and raising on that would throw
    away a whole measurement over an alignment detail the metrics do not care about.
    """
    n = min(a.shape[0], b.shape[0])
    return np.asarray(a[:n], dtype=np.float64), np.asarray(b[:n], dtype=np.float64)


def erle_db(mic: NDArray, cancelled: NDArray) -> float:
    """Echo Return Loss Enhancement: ``10*log10(mic_power / cancelled_power)``.

    HIGHER IS BETTER. 0 dB means no cancellation at all. Returns ``inf`` for a silent
    residual — read that as "perfect on this metric alone", never as "best backend"; see the
    module docstring.
    """
    mic_f, cancelled_f = common_length(mic, cancelled)
    cancelled_power = float(np.mean(cancelled_f**2))
    if cancelled_power == 0.0:
        return float("inf")
    mic_power = float(np.mean(mic_f**2))
    if mic_power == 0.0:
        return float("-inf")
    return 10.0 * float(np.log10(mic_power / cancelled_power))


def double_talk_attenuation_db(near_end_alone: NDArray, cancelled: NDArray) -> float:
    """``20*log10(rms(cancelled) / rms(near_end_alone))``.

    0 dB IS PERFECT — the user's voice passed through untouched. Negative means the canceller
    attenuated the user. -40 dB is near-total loss of the user.

    Raises:
        ValueError: if ``near_end_alone`` is silent. The reference is degenerate and a ratio
            against it says nothing about the backend.
    """
    near_f, cancelled_f = common_length(near_end_alone, cancelled)
    near_rms = float(np.sqrt(np.mean(near_f**2)))
    if near_rms == 0.0:
        raise ValueError(
            "near_end_alone is all zeros; double-talk attenuation is undefined against a "
            "silent reference"
        )
    cancelled_rms = float(np.sqrt(np.mean(cancelled_f**2)))
    if cancelled_rms == 0.0:
        return float("-inf")
    return 20.0 * float(np.log10(cancelled_rms / near_rms))


def added_latency_ms(
    reference: NDArray, captured: NDArray, sample_rate: int = SAMPLE_RATE
) -> float:
    """Delay of ``captured`` relative to ``reference``, via cross-correlation argmax.

    Positive means ``captured`` lags ``reference`` — the backend added latency. Negative means
    it leads. The sign is kept rather than absolute-valued because a lead is a symptom (clock
    drift, a mis-set buffer) that reads very differently from added processing delay.
    """
    reference_f, captured_f = common_length(reference, captured)
    correlation = np.correlate(captured_f, reference_f, mode="full")
    lag_samples = int(np.argmax(correlation)) - (reference_f.shape[0] - 1)
    return lag_samples / sample_rate * 1000.0
