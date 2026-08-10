"""The bench CLI — argument handling, guards, and the near-end contract.

Everything here runs headlessly: the backend is stubbed, so the CLI's decisions are tested
without a microphone. Only an actual measurement needs hardware.
"""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
import pytest

import edith.voice.aec_bench.__main__ as cli
from edith.voice.duplex import SAMPLE_RATE, DuplexUnavailable


def write_wav(path: Path, samples: np.ndarray, rate: int = SAMPLE_RATE, channels: int = 1) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(samples.astype(np.int16).tobytes())
    return path


def test_unavailable_backend_is_reported_not_raised(capsys, tmp_path: Path) -> None:
    """A backend that cannot start must not abort the run with a traceback.

    The bench exists to collect whatever numbers are still obtainable; one missing
    dependency must not cost the other backend's measurement.
    """
    code = cli.main(
        ["--backend", "speex", "--hardware", "macbook", "--seconds", "1",
         "--out-dir", str(tmp_path)]
    )
    assert code == 1
    assert "unavailable" in capsys.readouterr().err


def test_short_run_warns_about_hidden_clock_drift(capsys, tmp_path: Path, monkeypatch) -> None:
    """Runs shorter than the minimum hide the drift they exist to expose."""
    monkeypatch.setattr(
        cli, "build_backend",
        lambda name, aec_on=True: (_ for _ in ()).throw(DuplexUnavailable("stubbed")),
    )
    cli.main(["--backend", "vpio", "--hardware", "macbook", "--seconds", "2",
              "--out-dir", str(tmp_path)])
    assert "clock drift" in capsys.readouterr().err


def test_missing_near_end_says_double_talk_is_skipped(capsys, tmp_path: Path, monkeypatch) -> None:
    """Silence about a skipped metric invites reading a null as a zero."""
    monkeypatch.setattr(
        cli, "build_backend",
        lambda name, aec_on=True: (_ for _ in ()).throw(DuplexUnavailable("stubbed")),
    )
    cli.main(["--backend", "vpio", "--hardware", "macbook", "--out-dir", str(tmp_path)])
    err = capsys.readouterr().err
    assert "double-talk" in err.lower()
    assert "second device" in err


def test_near_end_at_the_wrong_sample_rate_is_refused(capsys, tmp_path: Path) -> None:
    """A silently-resampled reference would corrupt the double-talk number invisibly."""
    bad = write_wav(tmp_path / "near.wav", np.zeros(1000, dtype=np.int16), rate=44100)
    code = cli.main(
        ["--backend", "vpio", "--hardware", "macbook", "--near-end", str(bad),
         "--out-dir", str(tmp_path)]
    )
    assert code == 2
    assert "16000 Hz" in capsys.readouterr().err


def test_stereo_near_end_is_refused(capsys, tmp_path: Path) -> None:
    stereo = write_wav(tmp_path / "st.wav", np.zeros(2000, dtype=np.int16), channels=2)
    code = cli.main(
        ["--backend", "vpio", "--hardware", "macbook", "--near-end", str(stereo),
         "--out-dir", str(tmp_path)]
    )
    assert code == 2
    assert "mono" in capsys.readouterr().err


def test_load_wav_16k_roundtrips_a_valid_file(tmp_path: Path) -> None:
    samples = (np.arange(500) % 100).astype(np.int16)
    got = cli.load_wav_16k(write_wav(tmp_path / "ok.wav", samples))
    assert np.array_equal(got, samples)


@pytest.mark.parametrize("name,seconds", [("chirp", 0.5), ("noise", 0.5)])
def test_stimulus_selection_returns_the_right_length(name: str, seconds: float) -> None:
    out = cli.build_stimulus(name, seconds)
    assert len(out) == int(seconds * SAMPLE_RATE)
    assert out.dtype == np.int16
