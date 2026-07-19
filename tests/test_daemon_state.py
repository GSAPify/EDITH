"""Runtime state machine (spec 01 §"Control API + pause semantics").

The daemon holds one RuntimeState: RUNNING | PAUSED | STOPPING, plus the
mutable ``active_skill`` / ``last_event`` labels the Control API's ``status``
surfaces. Transitions: pause -> PAUSED, resume -> RUNNING, kill -> STOPPING.

Tested directly (no bus, no socket): the transitions, the ``is_paused`` view
Brain reads, and the illegal-transition guard (you cannot resume/pause a
STOPPING daemon — kill is terminal).
"""

from __future__ import annotations

import pytest

from edith.daemon.state import DaemonState, RuntimeState


def test_initial_state_is_running():
    state = RuntimeState()
    assert state.state is DaemonState.RUNNING
    assert state.is_paused is False
    assert state.active_skill is None
    assert state.last_event is None


def test_state_values_serialize_lowercase():
    # status surfaces these strings to the menu-bar; the task locks "running"/"paused".
    assert DaemonState.RUNNING.value == "running"
    assert DaemonState.PAUSED.value == "paused"
    assert DaemonState.STOPPING.value == "stopping"


def test_pause_enters_paused():
    state = RuntimeState()
    state.pause()
    assert state.state is DaemonState.PAUSED
    assert state.is_paused is True


def test_resume_returns_to_running():
    state = RuntimeState()
    state.pause()
    state.resume()
    assert state.state is DaemonState.RUNNING
    assert state.is_paused is False


def test_kill_enters_stopping():
    state = RuntimeState()
    state.kill()
    assert state.state is DaemonState.STOPPING
    assert state.is_paused is False


def test_is_stopping_view():
    """is_stopping is True only while STOPPING — Brain reads it to skip a pass during
    shutdown (spec 10 review: prevents a late utterance hitting a closing Kuzu handle)."""
    state = RuntimeState()
    assert state.is_stopping is False  # RUNNING
    state.pause()
    assert state.is_stopping is False  # PAUSED
    state.resume()
    state.kill()
    assert state.is_stopping is True  # STOPPING


def test_pause_is_idempotent():
    state = RuntimeState()
    state.pause()
    state.pause()
    assert state.state is DaemonState.PAUSED


def test_cannot_pause_or_resume_once_stopping():
    # kill is terminal; the daemon is shutting down and must not re-enter RUNNING/PAUSED.
    state = RuntimeState()
    state.kill()
    with pytest.raises(ValueError):
        state.pause()
    with pytest.raises(ValueError):
        state.resume()
    assert state.state is DaemonState.STOPPING


def test_active_skill_and_last_event_are_mutable_labels():
    state = RuntimeState()
    state.active_skill = "pr-review"
    state.last_event = "brain.decision"
    assert state.active_skill == "pr-review"
    assert state.last_event == "brain.decision"
