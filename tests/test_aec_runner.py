"""The bench runner — drives a DuplexAudio through one measurement run.

Exercised entirely through ``FakeDuplex``, so the whole orchestration path is testable with no
audio hardware: play a stimulus, drain cancelled frames, archive the signals, compute metrics,
append a result row.
"""

from __future__ import annotations

import json
import wave
from pathlib import Path

import numpy as np
import pytest

from edith.voice.aec_bench.fake import FakeDuplex
from edith.voice.aec_bench.runner import (
    SilentMicError,
    capture_during_playback,
    render,
    run_once,
)
from edith.voice.duplex import FRAME_SAMPLES, SAMPLE_RATE


def write_wav(path: Path, samples: np.ndarray) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(samples.astype(np.int16).tobytes())
    return path


def loud(n: int, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return (rng.standard_normal(n) * 3000).astype(np.int16)


def test_playback_and_capture_advance_together(tmp_path: Path) -> None:
    """Playback MUST be chunked frame-by-frame, not written in one shot.

    Writing the whole stimulus up front would let it drain before the first frame is read, so
    the "cancelled" capture would contain no echo at all and ERLE would come out spectacular.
    That is a bug producing a great number rather than an obvious failure, so it is pinned here.
    """
    mic = write_wav(tmp_path / "mic.wav", loud(FRAME_SAMPLES * 5))
    duplex = FakeDuplex(mic)
    stimulus = loud(FRAME_SAMPLES * 5, seed=1)

    captured = capture_during_playback(duplex, stimulus)

    assert len(duplex.played) == 5, "one play() per captured frame, interleaved"
    assert len(captured) == FRAME_SAMPLES * 5


def test_a_silent_mic_fails_loudly_instead_of_scoring_perfectly(tmp_path: Path) -> None:
    """A muted speaker would score superb ERLE and produce the wrong conclusion.

    The ERLE trap in a different costume: no echo reaching the mic looks identical to perfect
    cancellation. It must raise rather than report a great number.
    """
    silent = write_wav(tmp_path / "silent.wav", np.zeros(FRAME_SAMPLES * 4, dtype=np.int16))
    duplex = FakeDuplex(silent)

    with pytest.raises(SilentMicError, match="speaker"):
        run_once(
            duplex,
            loud(FRAME_SAMPLES * 4),
            backend="fake",
            hardware="macbook",
            stimulus_name="noise",
            out_dir=tmp_path / "out",
        )


def test_run_once_archives_audio_and_appends_a_result_row(tmp_path: Path) -> None:
    mic = write_wav(tmp_path / "mic.wav", loud(FRAME_SAMPLES * 6))
    duplex = FakeDuplex(mic)
    out_dir = tmp_path / "out"

    result = run_once(
        duplex,
        loud(FRAME_SAMPLES * 6, seed=2),
        backend="fake",
        hardware="macbook",
        stimulus_name="speech_shaped_noise",
        out_dir=out_dir,
        notes="unit test",
    )

    wav_dir = Path(result.wav_dir)
    assert (wav_dir / "reference.wav").exists()
    assert (wav_dir / "cancelled.wav").exists(), "without the archive a bad run is undebuggable"

    rows = (out_dir / "results.jsonl").read_text().strip().splitlines()
    assert len(rows) == 1
    row = json.loads(rows[0])
    assert row["backend"] == "fake"
    assert row["hardware"] == "macbook"
    assert row["notes"] == "unit test"
    assert isinstance(row["erle_db"], float)


def test_double_talk_is_only_reported_when_a_near_end_reference_is_given(tmp_path: Path) -> None:
    """A single-talk run has no near-end signal, so the field must be null, not a fabricated 0."""
    mic = write_wav(tmp_path / "mic.wav", loud(FRAME_SAMPLES * 4))
    result = run_once(
        FakeDuplex(mic),
        loud(FRAME_SAMPLES * 4, seed=3),
        backend="fake",
        hardware="airpods",
        stimulus_name="chirp",
        out_dir=tmp_path / "out",
    )
    assert result.double_talk_attenuation_db is None


def test_render_spells_out_the_opposing_sign_conventions(tmp_path: Path) -> None:
    """ERLE higher-is-better and double-talk zero-is-better pull opposite ways.

    A report that omits this is misread at a glance, which is how a losing backend gets picked.
    """
    mic = write_wav(tmp_path / "mic.wav", loud(FRAME_SAMPLES * 4))
    result = run_once(
        FakeDuplex(mic),
        loud(FRAME_SAMPLES * 4, seed=4),
        backend="fake",
        hardware="macbook",
        stimulus_name="chirp",
        out_dir=tmp_path / "out",
        near_end_alone=loud(FRAME_SAMPLES * 4, seed=5),
    )

    report = render(result)
    assert "higher is better" in report
    assert "0 is perfect" in report
