"""Tests for Endpointer (edith/voice/endpointing.py) — pure logic, no audio deps.

All tests feed synthetic RMS sequences to verify the state-machine transitions.
Numeric calibration (what threshold value catches real speech) is NOT tested here;
see the module docstring in endpointing.py for live-calibration guidance.
"""

from __future__ import annotations

import pytest

from edith.voice.endpointing import Endpointer

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SILENCE_MS = 800.0
_HARD_MAX_MS = 15000.0
_THRESHOLD = 500.0
_FRAME_MS = 80.0

# Frames needed to fill the silence window (800 ms / 80 ms = 10 frames).
_SILENCE_FRAMES = int(_SILENCE_MS / _FRAME_MS)  # 10

# A clearly sub-threshold RMS value (deep silence).
_QUIET = 0.0
# A clearly supra-threshold RMS value (speech).
_LOUD = 1000.0


def _make() -> Endpointer:
    return Endpointer(
        silence_ms=_SILENCE_MS,
        hard_max_ms=_HARD_MAX_MS,
        threshold=_THRESHOLD,
        frame_ms=_FRAME_MS,
    )


def _feed_n(ep: Endpointer, rms: float, n: int) -> list[bool]:
    """Feed *n* identical frames; return all return values."""
    return [ep.feed(rms) for _ in range(n)]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_ends_after_silence_following_speech() -> None:
    """feed() returns True once silence_ms of sub-threshold frames follow speech."""
    ep = _make()

    # Speak for a few frames.
    results = _feed_n(ep, _LOUD, 5)
    assert not any(results), "should not end during speech"

    # Now go quiet — must fire exactly when the silence window fills.
    for i in range(_SILENCE_FRAMES - 1):
        ended = ep.feed(_QUIET)
        assert not ended, f"ended prematurely at silence frame {i + 1}/{_SILENCE_FRAMES}"

    # The last frame completes the silence window.
    assert ep.feed(_QUIET) is True, "did not end after full silence window"


def test_does_not_end_during_leading_silence() -> None:
    """Leading silence (before speech starts) must NOT trigger endpointing.

    NON-VACUOUS: we feed more frames than the silence_ms window would need,
    confirm no end fires, then verify speech DOES trigger the speech flag.
    """
    ep = _make()

    # Feed twice as many quiet frames as the silence window requires.
    many_quiet = _SILENCE_FRAMES * 2
    results = _feed_n(ep, _QUIET, many_quiet)

    assert not any(results), (
        "Endpointer fired during leading silence — must wait for speech first"
    )
    assert not ep.started, "started flag should be False with only quiet frames"

    # Confirm speech detection still works after leading silence.
    ep.feed(_LOUD)
    assert ep.started, "started flag should flip on first loud frame"


def test_respects_hard_max_with_continuous_speech() -> None:
    """Hard-max fires even when speech never goes quiet (owner keeps talking)."""
    ep = _make()

    # Calculate how many loud frames it takes to exceed hard_max_ms.
    frames_to_max = int(_HARD_MAX_MS / _FRAME_MS)  # 15000/80 = 187.5 → 187 frames not yet over

    results = _feed_n(ep, _LOUD, frames_to_max)
    assert not results[-1], "should not have ended at exactly the frame boundary (not yet exceeded)"

    # One more frame pushes elapsed over hard_max_ms.
    assert ep.feed(_LOUD) is True, "hard-max did not fire after hard_max_ms of continuous speech"


def test_reset_clears_all_state() -> None:
    """reset() returns the Endpointer to a clean initial state."""
    ep = _make()

    # Advance into a mid-speech state.
    _feed_n(ep, _LOUD, 5)
    _feed_n(ep, _QUIET, 3)

    assert ep.elapsed_ms > 0
    assert ep.started

    ep.reset()

    assert ep.elapsed_ms == 0.0
    assert not ep.started

    # After reset, leading silence must not trigger endpointing (state really cleared).
    results = _feed_n(ep, _QUIET, _SILENCE_FRAMES)
    assert not any(results), "leading silence after reset triggered end — state not fully cleared"


def test_short_pause_does_not_end_utterance() -> None:
    """A pause SHORTER than silence_ms must NOT end the utterance.

    This is the core "don't cut me off when I pause to think" property.
    NON-VACUOUS: we feed exactly (silence_frames - 1) quiet frames between
    two bursts of speech and assert no end fires; then extend the silence to
    the full window and confirm it DOES end.
    """
    ep = _make()

    # First burst of speech.
    _feed_n(ep, _LOUD, 3)

    # Pause that is ONE frame short of the silence window.
    short_pause = _SILENCE_FRAMES - 1  # 9 frames = 720 ms < 800 ms
    assert short_pause > 0, "test precondition: short_pause must be at least 1 frame"

    results = _feed_n(ep, _QUIET, short_pause)
    assert not any(results), (
        f"Utterance ended after only {short_pause * _FRAME_MS:.0f} ms of silence "
        f"(threshold is {_SILENCE_MS:.0f} ms) — owner would be cut off mid-thought"
    )

    # Resume speech — still no end.
    results = _feed_n(ep, _LOUD, 3)
    assert not any(results), "ended during resumed speech after a short pause"

    # Now a full silence window ends it.
    results = _feed_n(ep, _QUIET, _SILENCE_FRAMES)
    assert results[-1] is True, "did not end after full silence window following resumed speech"


def test_hard_max_fires_even_without_speech() -> None:
    """Hard-max must fire even if the owner never spoke (pure silence throughout)."""
    ep = _make()

    frames_to_max = int(_HARD_MAX_MS / _FRAME_MS)
    _feed_n(ep, _QUIET, frames_to_max)

    assert ep.feed(_QUIET) is True, "hard-max did not fire during leading-only silence"
    assert not ep.started, "started should still be False — no speech was ever detected"


def test_elapsed_ms_tracks_correctly() -> None:
    """elapsed_ms increments by frame_ms with each feed() call."""
    ep = _make()

    assert ep.elapsed_ms == pytest.approx(0.0)
    ep.feed(_QUIET)
    assert ep.elapsed_ms == pytest.approx(_FRAME_MS)
    ep.feed(_LOUD)
    assert ep.elapsed_ms == pytest.approx(2 * _FRAME_MS)

    ep.reset()
    assert ep.elapsed_ms == pytest.approx(0.0)


def test_trailing_silence_resets_on_resumed_speech() -> None:
    """Silence counter resets when the owner resumes talking after a gap."""
    ep = _make()

    # Speech, then a partial pause (not yet triggering end).
    _feed_n(ep, _LOUD, 3)
    _feed_n(ep, _QUIET, _SILENCE_FRAMES - 1)  # 9 frames — not enough

    # Resume speech — silence counter must reset.
    ep.feed(_LOUD)

    # Now silence again: need a FULL window from this point.
    results = _feed_n(ep, _QUIET, _SILENCE_FRAMES - 1)
    assert not any(results), (
        "ended before silence window completed after resumed speech — "
        "silence counter was not reset by the resumed-speech frame"
    )
    assert ep.feed(_QUIET) is True, "did not end after full silence window post-resume"
