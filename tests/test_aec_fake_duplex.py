"""Tests for FakeDuplex (AEC bench plan 1, worker 1 lane): WAV replay as fake capture.

TDD red->green: these drive edith/voice/aec_bench/fake.py.

FakeDuplex exists so runner.py and the metric math can be exercised end-to-end with NO
hardware. Every case below pins a decision that would silently corrupt a measurement if it
went the other way:

  1. exact frame count for a known-length WAV (the framing must match live.py's 1280).
  2. a trailing partial frame is DROPPED, not zero-padded (padding inflates ERLE with fake
     silence the canceller never saw).
  3. a non-16 kHz WAV raises ValueError naming BOTH rates (a silently-resampled fixture
     would corrupt every metric downstream).
  4. play() records instead of emitting, so a test can assert the runner sent the stimulus.
  5. close() is idempotent.
  6. frames() after close() raises DuplexUnavailable, eagerly.
"""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
import pytest

from edith.voice.aec_bench.fake import FakeDuplex
from edith.voice.duplex import FRAME_SAMPLES, SAMPLE_RATE, DuplexUnavailable


def write_wav(path: Path, samples: np.ndarray, sample_rate: int = SAMPLE_RATE) -> Path:
    """Write a 16-bit mono WAV fixture. Built in tmp_path — no binary fixtures committed."""
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(samples.astype(np.int16).tobytes())
    return path


def test_frames_yields_exact_frame_count_for_a_whole_number_of_frames(tmp_path: Path) -> None:
    samples = np.arange(FRAME_SAMPLES * 3, dtype=np.int16)
    duplex = FakeDuplex(write_wav(tmp_path / "mic.wav", samples))

    frames = list(duplex.frames())

    assert len(frames) == 3
    assert all(frame.shape == (FRAME_SAMPLES,) for frame in frames)
    assert all(frame.dtype == np.int16 for frame in frames)
    np.testing.assert_array_equal(np.concatenate(frames), samples)


def test_frames_drops_a_trailing_partial_frame_rather_than_padding_it(tmp_path: Path) -> None:
    """Zero-padding would add silence the canceller never saw and inflate ERLE."""
    samples = np.arange(FRAME_SAMPLES * 2 + 17, dtype=np.int16)
    duplex = FakeDuplex(write_wav(tmp_path / "mic.wav", samples))

    frames = list(duplex.frames())

    assert len(frames) == 2
    np.testing.assert_array_equal(np.concatenate(frames), samples[: FRAME_SAMPLES * 2])


def test_a_wav_at_the_wrong_sample_rate_raises_naming_both_rates(tmp_path: Path) -> None:
    samples = np.zeros(FRAME_SAMPLES, dtype=np.int16)
    path = write_wav(tmp_path / "mic8k.wav", samples, sample_rate=8000)

    with pytest.raises(ValueError, match=r"(?s)8000.*16000|16000.*8000"):
        FakeDuplex(path)


def test_play_records_the_pcm_it_was_given_and_emits_no_audio(tmp_path: Path) -> None:
    duplex = FakeDuplex(write_wav(tmp_path / "mic.wav", np.zeros(FRAME_SAMPLES, dtype=np.int16)))

    assert duplex.played == []
    duplex.play(b"\x01\x02")
    duplex.play(b"\x03\x04")

    assert duplex.played == [b"\x01\x02", b"\x03\x04"]


def test_close_is_idempotent(tmp_path: Path) -> None:
    duplex = FakeDuplex(write_wav(tmp_path / "mic.wav", np.zeros(FRAME_SAMPLES, dtype=np.int16)))

    duplex.close()
    duplex.close()  # must not raise


def test_frames_after_close_raises_duplex_unavailable(tmp_path: Path) -> None:
    """Eagerly: frames() itself raises, not the first next() on a lazily-started generator."""
    duplex = FakeDuplex(write_wav(tmp_path / "mic.wav", np.zeros(FRAME_SAMPLES, dtype=np.int16)))
    duplex.close()

    with pytest.raises(DuplexUnavailable):
        duplex.frames()
