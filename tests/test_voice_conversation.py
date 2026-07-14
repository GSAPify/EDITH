"""Tests for ConversationWindow (spec 03 §Follow-up window).

Pure state-machine tests — no real audio, no clock sleeping.
Deterministic time via the ``now=[t]`` + ``clock=lambda: now[0]`` idiom
(same pattern as ``tests/test_voice_io.py``).
"""

from __future__ import annotations

from edith.voice.conversation import (
    ConversationWindow,
    ConvState,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_win(window: float = 10.0) -> tuple[ConversationWindow, list[float]]:
    """Return (window, now_cell) — caller mutates now[0] to advance time."""
    now: list[float] = [0.0]
    win = ConversationWindow(window_seconds=window, clock=lambda: now[0])
    return win, now


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_default_state_is_idle() -> None:
    """Before any reply, the window is IDLE and accepts no follow-ups."""
    win, now = _make_win()
    assert win.state is ConvState.IDLE
    assert win.accepts_followup(now[0]) is False


def test_on_reply_finished_enters_conversing() -> None:
    """on_reply_finished() transitions IDLE → CONVERSING."""
    win, now = _make_win()
    win.on_reply_finished(now[0])
    assert win.state is ConvState.CONVERSING


def test_accepts_followup_true_inside_window() -> None:
    """accepts_followup() returns True while inside the window."""
    win, now = _make_win(window=10.0)
    win.on_reply_finished(now[0])  # deadline = 0 + 10 = 10.0

    now[0] = 5.0  # well inside
    assert win.accepts_followup(now[0]) is True
    assert win.state is ConvState.CONVERSING  # no side-effect inside window


def test_accepts_followup_false_past_deadline() -> None:
    """accepts_followup() returns False when now >= deadline."""
    win, now = _make_win(window=10.0)
    win.on_reply_finished(now[0])  # deadline = 10.0

    now[0] = 10.0  # exactly at deadline — should be False (>= check)
    assert win.accepts_followup(now[0]) is False


def test_past_deadline_query_flips_state_to_idle() -> None:
    """Querying accepts_followup() past the deadline transitions back to IDLE."""
    win, now = _make_win(window=10.0)
    win.on_reply_finished(now[0])
    assert win.state is ConvState.CONVERSING

    now[0] = 15.0  # past deadline
    result = win.accepts_followup(now[0])
    assert result is False
    assert win.state is ConvState.IDLE  # side-effect: state flipped


def test_on_utterance_resets_extends_deadline() -> None:
    """on_utterance() extends the deadline while CONVERSING."""
    win, now = _make_win(window=10.0)
    now[0] = 100.0
    win.on_reply_finished(now[0])  # deadline = 110.0

    now[0] = 108.0  # inside window, 2 s left
    win.on_utterance(now[0])  # new deadline = 108 + 10 = 118.0

    now[0] = 115.0  # would have expired under old deadline but not new one
    assert win.accepts_followup(now[0]) is True


def test_on_utterance_noop_when_idle() -> None:
    """on_utterance() does nothing when state is IDLE."""
    win, now = _make_win(window=10.0)
    assert win.state is ConvState.IDLE
    win.on_utterance(now[0])  # must not raise or change state
    assert win.state is ConvState.IDLE


def test_reset_forces_idle() -> None:
    """reset() returns the window to IDLE regardless of current state."""
    win, now = _make_win(window=10.0)
    win.on_reply_finished(now[0])
    assert win.state is ConvState.CONVERSING

    win.reset()
    assert win.state is ConvState.IDLE
    assert win.accepts_followup(now[0]) is False


def test_reset_from_idle_is_safe() -> None:
    """reset() is idempotent — safe to call when already IDLE."""
    win, now = _make_win()
    win.reset()  # must not raise
    assert win.state is ConvState.IDLE


def test_multiple_reply_cycles() -> None:
    """Window can be re-entered across multiple reply/silence cycles."""
    win, now = _make_win(window=10.0)

    # First reply
    now[0] = 0.0
    win.on_reply_finished(now[0])
    now[0] = 5.0
    assert win.accepts_followup(now[0]) is True

    # Deadline expires
    now[0] = 11.0
    assert win.accepts_followup(now[0]) is False
    assert win.state is ConvState.IDLE

    # Second reply
    now[0] = 20.0
    win.on_reply_finished(now[0])
    now[0] = 25.0
    assert win.accepts_followup(now[0]) is True
    assert win.state is ConvState.CONVERSING


def test_default_clock_used_when_now_is_none() -> None:
    """Methods accept now=None and fall through to the injected clock."""
    now: list[float] = [0.0]
    win = ConversationWindow(window_seconds=10.0, clock=lambda: now[0])

    win.on_reply_finished()  # now=None → clock() called
    assert win.state is ConvState.CONVERSING

    now[0] = 5.0
    assert win.accepts_followup() is True  # now=None → clock() called

    now[0] = 8.0
    win.on_utterance()  # now=None → clock() called; deadline extended to 18.0

    now[0] = 15.0
    assert win.accepts_followup() is True  # still inside extended window
