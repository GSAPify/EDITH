"""Half-duplex mic-gate state machine (spec 03 §Barge-in / self-echo fix).

The live mic loop can't be tested headlessly (it needs a real device), but its
gate decision is a pure function — so test THAT, leaving only raw sounddevice
reads in the untestable shell. This is the regression guard for the self-echo
feedback loop (EDITH re-triggering on her own TTS).

``edith.voice.live`` keeps every heavy import inside functions, so importing the
module here is safe without the ``[voice]`` extra.
"""

from __future__ import annotations

from edith.voice.live import _followup_poll, _followup_transition, _frames_for_seconds, _gate_action


class _FollowupSignalSpy:
    """Records consume_followup_ready() calls — the minimal seam ``_followup_poll``
    needs, satisfied structurally by a real ``VoiceIO`` (spec 03 §Follow-up)."""

    def __init__(self, *, ready: bool = False, paused: bool = False) -> None:
        self._ready = ready
        self.is_paused = paused
        self.consume_calls = 0

    def consume_followup_ready(self) -> bool:
        self.consume_calls += 1
        return self._ready


def test_idle_processes_frames() -> None:
    assert _gate_action(is_speaking=False, was_speaking=False) == ("process", False)


def test_speaking_skips_and_latches() -> None:
    assert _gate_action(is_speaking=True, was_speaking=False) == ("skip", True)
    assert _gate_action(is_speaking=True, was_speaking=True) == ("skip", True)


def test_stop_speaking_triggers_one_flush_then_resumes() -> None:
    # Transition speaking→idle: flush the TTS tail + reset the detector, exactly once.
    action, was = _gate_action(is_speaking=False, was_speaking=True)
    assert action == "flush" and was is False
    # Next tick is a normal process (no repeated flush).
    assert _gate_action(is_speaking=False, was_speaking=was) == ("process", False)


def test_full_cycle() -> None:
    """idle → speak (skip×N) → stop (flush once) → idle (process)."""
    was = False
    seq = []
    for speaking in (False, True, True, True, False, False):
        action, was = _gate_action(speaking, was)
        seq.append(action)
    assert seq == ["process", "skip", "skip", "skip", "flush", "process"]


# ---------------------------------------------------------------------------
# _frames_for_seconds — rounds UP so a flush/preroll window is never short-changed.
# ---------------------------------------------------------------------------


def test_frames_for_seconds_rounds_up_not_down() -> None:
    """0.3s at 16 kHz/1280-sample frames is 3.75 frames — must round up to 4, not
    truncate to 3 (which would flush only ~240ms of the configured 300ms)."""
    assert _frames_for_seconds(0.3, sample_rate=16000, frame_samples=1280) == 4


def test_frames_for_seconds_exact_multiple_stays_exact() -> None:
    """An exact multiple must not be bumped up an extra frame by the rounding."""
    assert _frames_for_seconds(0.08, sample_rate=16000, frame_samples=1280) == 1


# ---------------------------------------------------------------------------
# _followup_transition — combines the half-duplex gate action with VoiceIO's
# one-shot response-completion signal (bounded voice behavior fix).
# ---------------------------------------------------------------------------


def test_followup_transition_narration_only_flush_does_not_open_window() -> None:
    """A normal "flush" (EDITH just stopped speaking) with no response armed —
    e.g. startup speech or session narration — must flush but never open the window."""
    assert _followup_transition("flush", response_ready=False, muted=False) == (True, False)


def test_followup_transition_response_ready_opens_window_on_flush() -> None:
    """A "flush" gate action WITH a response armed must flush AND open the window."""
    assert _followup_transition("flush", response_ready=True, muted=False) == (True, True)


def test_followup_transition_missed_speaking_edge_still_flushes_and_opens() -> None:
    """The gate reports "process" (the speaking→idle edge was missed between polls —
    e.g. TTS finished entirely during a blocking capture/transcribe), but the
    response-completion signal fired anyway — must still flush + open the window."""
    assert _followup_transition("process", response_ready=True, muted=False) == (True, True)


def test_followup_transition_muted_completion_does_not_open_window() -> None:
    """A completed response while muted must still flush (drain the tail) but never open."""
    assert _followup_transition("flush", response_ready=True, muted=True) == (True, False)
    assert _followup_transition("process", response_ready=True, muted=True) == (True, False)


def test_followup_transition_idle_process_does_nothing() -> None:
    """Normal idle processing with nothing armed: no flush, no window."""
    assert _followup_transition("process", response_ready=False, muted=False) == (False, False)


def test_followup_transition_skip_action_never_opens_without_a_signal() -> None:
    """A "skip" gate action (still speaking) with no signal must do nothing."""
    assert _followup_transition("skip", response_ready=False, muted=False) == (False, False)


# ---------------------------------------------------------------------------
# _followup_poll — must not consume the one-shot signal while still speaking
# (round-2 concurrency fix: VoiceIO only exposes a completed response once
# EVERY tracked utterance is idle, so peeking during "skip" is never useful
# and risks a spurious/duplicate read of a signal that isn't ready yet).
# ---------------------------------------------------------------------------


def test_followup_poll_does_not_consume_the_signal_while_still_speaking() -> None:
    spy = _FollowupSignalSpy(ready=True)
    assert _followup_poll(spy, "skip") == (False, False)
    assert spy.consume_calls == 0  # never even peeked while still speaking


def test_followup_poll_consumes_and_opens_on_flush() -> None:
    spy = _FollowupSignalSpy(ready=True)
    assert _followup_poll(spy, "flush") == (True, True)
    assert spy.consume_calls == 1


def test_followup_poll_consumes_on_process_for_the_missed_edge_case() -> None:
    spy = _FollowupSignalSpy(ready=True)
    assert _followup_poll(spy, "process") == (True, True)
    assert spy.consume_calls == 1


def test_followup_poll_respects_muted_after_consuming() -> None:
    spy = _FollowupSignalSpy(ready=True, paused=True)
    assert _followup_poll(spy, "flush") == (True, False)
    assert spy.consume_calls == 1
