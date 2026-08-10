"""Pure resampling / framing helpers for the duplex backends.

VPIO imposes 48 kHz and 4800-sample buffers; the wake detector needs 16 kHz in 1280-sample
frames. Neither rate nor framing lines up, so this arithmetic runs on every captured buffer —
which makes it exactly the kind of code that is worth testing without a microphone attached.
"""

from __future__ import annotations

import numpy as np

from edith.voice.duplex.resample import (
    DECIMATION,
    decimate_to_16k,
    float_to_int16,
    frames_of,
    int16_to_float,
    reblock,
)


def test_decimation_ratio_is_exactly_three() -> None:
    assert DECIMATION == 3


def test_decimate_returns_one_third_of_the_samples() -> None:
    out = decimate_to_16k(np.zeros(4800, dtype=np.float64))
    assert len(out) == 1600  # 100 ms at 48 kHz -> 100 ms at 16 kHz


def test_decimate_handles_an_empty_buffer() -> None:
    assert decimate_to_16k(np.zeros(0, dtype=np.float64)).size == 0


def test_decimate_attenuates_a_tone_above_the_new_nyquist() -> None:
    """Naive 3:1 decimation would fold >8 kHz content back into the speech band.

    A 12 kHz tone is above the 8 kHz Nyquist of the decimated rate. Without the anti-alias
    filter it would reappear as a loud in-band alias and corrupt both the wake detector and
    any measurement taken through this path.
    """
    sr = 48000
    t = np.arange(sr // 10) / sr
    tone_12k = np.sin(2 * np.pi * 12000.0 * t)
    tone_1k = np.sin(2 * np.pi * 1000.0 * t)

    high_out = decimate_to_16k(tone_12k)
    low_out = decimate_to_16k(tone_1k)

    high_rms = float(np.sqrt(np.mean(high_out**2)))
    low_rms = float(np.sqrt(np.mean(low_out**2)))
    assert high_rms < low_rms / 4, "out-of-band tone must be attenuated, not aliased through"


def test_float_to_int16_clamps_instead_of_wrapping() -> None:
    """A value above 1.0 must saturate, not wrap to a large negative int16.

    Wrapping turns a loud moment into a discontinuity that reads as a click to the wake
    detector — a corruption that only appears at high volume.
    """
    out = float_to_int16(np.array([2.0, -2.0, 0.0]))
    assert out[0] == 32767
    assert out[1] == -32767
    assert out[2] == 0


def test_int16_float_roundtrip_is_close() -> None:
    original = np.array([0, 1000, -1000, 32767], dtype=np.int16)
    back = float_to_int16(int16_to_float(original))
    assert np.max(np.abs(back.astype(int) - original.astype(int))) <= 1


def test_reblock_carries_the_remainder_instead_of_dropping_it() -> None:
    """VPIO's 4800 and the detector's 1280 never line up; the tail must survive.

    Dropping it loses ~4 ms of speech per buffer; zero-padding injects fake silence the
    endpointer would read as a pause.
    """
    buffer = np.arange(4800, dtype=np.int16)
    frames, tail = reblock(buffer, 1280)

    assert len(frames) == 3  # 3 * 1280 = 3840
    assert len(tail) == 960  # 4800 - 3840, carried forward
    assert frames[0][0] == 0
    assert tail[0] == 3840


def test_reblock_of_an_exact_multiple_leaves_no_tail() -> None:
    frames, tail = reblock(np.zeros(2560, dtype=np.int16), 1280)
    assert len(frames) == 2
    assert tail.size == 0


def test_reblock_shorter_than_one_frame_is_all_tail() -> None:
    frames, tail = reblock(np.zeros(500, dtype=np.int16), 1280)
    assert frames == []
    assert len(tail) == 500


def test_frames_of_drops_a_trailing_partial_frame() -> None:
    got = list(frames_of(np.zeros(1280 * 2 + 7, dtype=np.int16), 1280))
    assert len(got) == 2
    assert all(len(f) == 1280 for f in got)
