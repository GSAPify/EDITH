"""Half-duplex mic-gate state machine (spec 03 §Barge-in / self-echo fix).

The live mic loop can't be tested headlessly (it needs a real device), but its
gate decision is a pure function — so test THAT, leaving only raw sounddevice
reads in the untestable shell. This is the regression guard for the self-echo
feedback loop (EDITH re-triggering on her own TTS).

``edith.voice.live`` keeps every heavy import inside functions, so importing the
module here is safe without the ``[voice]`` extra.
"""

from __future__ import annotations

from edith.voice.live import _gate_action


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
