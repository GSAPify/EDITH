"""SessionBus (spec 04 §Step 2): normalize raw transcript records → bus events.

Fully headless — synthetic records go straight into ``ingest``; no ``~/.claude``,
no files, no watchdog. The collector's file I/O is tested separately.
"""

from __future__ import annotations

import pytest

from edith.bus import Event, EventBus
from edith.daemon.state import RuntimeState
from edith.session.bus import SessionBus

pytestmark = pytest.mark.asyncio


class _Sink:
    """Collects everything published on a topic."""

    def __init__(self, bus: EventBus, topic: str) -> None:
        self.events: list[Event] = []
        bus.subscribe(topic, self._on)

    async def _on(self, event: Event) -> None:
        self.events.append(event)

    @property
    def payloads(self) -> list[dict]:
        return [e.payload for e in self.events]


def _record(**over: object) -> dict:
    base = {
        "type": "user",
        "sessionId": "sess-abc",
        "cwd": "/Users/x/gitstuff/agents",
        "gitBranch": "master",
        "timestamp": "2026-07-09T10:00:00.000Z",
        "promptSource": "typed",
        "message": {"role": "user", "content": "review the failing dag"},
    }
    base.update(over)
    return base


async def test_prompt_record_publishes_event_and_state() -> None:
    bus = EventBus()
    events, states = _Sink(bus, "session.event"), _Sink(bus, "session.state")
    sb = SessionBus(bus)

    await sb.ingest(_record())

    # first-seen session synthesizes a start, then the prompt event.
    kinds = [p["kind"] for p in events.payloads]
    assert kinds == ["start", "prompt"]
    prompt = events.payloads[-1]
    assert prompt["session_id"] == "sess-abc"
    assert prompt["repo"] == "agents"
    assert "review the failing dag" in prompt["summary"]
    # state reflects working, current_action set, repo carried.
    assert states.payloads[-1]["state"] == "working"
    assert states.payloads[-1]["repo"] == "agents"


async def test_tool_use_record_names_the_tool() -> None:
    bus = EventBus()
    events = _Sink(bus, "session.event")
    sb = SessionBus(bus)
    await sb.ingest(_record(type="user"))  # establish session (start + prompt)
    events.events.clear()

    await sb.ingest(
        {
            "type": "assistant",
            "sessionId": "sess-abc",
            "cwd": "/Users/x/gitstuff/agents",
            "gitBranch": "master",
            "message": {
                "role": "assistant",
                "content": [{"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}],
            },
        }
    )

    assert events.payloads[-1]["kind"] == "tool_use"
    assert "Bash" in events.payloads[-1]["summary"]


async def test_secret_is_redacted_before_publish() -> None:
    bus = EventBus()
    events, states = _Sink(bus, "session.event"), _Sink(bus, "session.state")
    sb = SessionBus(bus)

    # A pasted Airflow-style error carrying a connection URI with a password.
    leak = "postgresql://admin:hunter2SECRET@db.internal:5432/airflow"
    await sb.ingest(_record(message={"role": "user", "content": f"got error: {leak}"}))

    for payload in (*events.payloads, *states.payloads):
        blob = str(payload)
        assert "hunter2SECRET" not in blob
        assert "hunter2" not in blob


async def test_error_result_sets_error_state() -> None:
    bus = EventBus()
    states = _Sink(bus, "session.state")
    events = _Sink(bus, "session.event")
    sb = SessionBus(bus)
    await sb.ingest(_record())  # establish session
    events.events.clear()
    states.events.clear()

    await sb.ingest(
        {
            "type": "user",
            "sessionId": "sess-abc",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "is_error": True, "content": "boom: exit 1"}
                ],
            },
        }
    )

    assert events.payloads[-1]["kind"] == "error"
    assert states.payloads[-1]["state"] == "error"


async def test_stop_record_goes_idle() -> None:
    bus = EventBus()
    states = _Sink(bus, "session.state")
    events = _Sink(bus, "session.event")
    sb = SessionBus(bus)
    await sb.ingest(_record())
    events.events.clear()
    states.events.clear()

    await sb.ingest({"type": "system", "sessionId": "sess-abc", "stopReason": "end_turn"})

    assert events.payloads[-1]["kind"] == "stop"
    assert states.payloads[-1]["state"] == "idle"


async def test_session_states_map_and_last_event() -> None:
    bus = EventBus()
    rs = RuntimeState()
    sb = SessionBus(bus, runtime_state=rs)

    await sb.ingest(_record())

    snap = sb.session_states()
    assert "sess-abc" in snap
    assert snap["sess-abc"]["state"] == "working"
    # Control API status surfaces the latest event.
    assert rs.last_event is not None
    assert "sess-abc"[:8] in rs.last_event or "agents" in rs.last_event


async def test_noise_record_is_ignored() -> None:
    bus = EventBus()
    events, states = _Sink(bus, "session.event"), _Sink(bus, "session.state")
    sb = SessionBus(bus)

    # UI-only record types carry no session activity → no publish at all.
    await sb.ingest({"type": "ai-title", "sessionId": "sess-abc", "title": "x"})
    await sb.ingest({"type": "pr-link", "sessionId": "sess-abc"})

    assert events.payloads == []
    assert states.payloads == []


async def test_summary_cap_never_leaves_a_partial_redaction_marker() -> None:
    bus = EventBus()
    events = _Sink(bus, "session.event")
    sb = SessionBus(bus)

    # Pad so the connection URI's [REDACTED] marker lands right on the 160-char cap.
    pad = "x" * 140
    leak = "postgresql://u:TopSecretPassword@db:5432/app"
    await sb.ingest(_record(message={"role": "user", "content": f"{pad} {leak}"}))

    summary = events.payloads[-1]["summary"]
    assert "TopSecretPassword" not in summary
    # No dangling "[RED"/"[REDACTE" fragment left by the cut.
    assert not summary.rstrip().endswith(("[R", "[RE", "[RED", "[REDACT", "[REDACTE"))


async def test_repo_is_null_when_not_a_repo_dir() -> None:
    bus = EventBus()
    events = _Sink(bus, "session.event")
    sb = SessionBus(bus)

    await sb.ingest(_record(cwd="/Users/x", gitBranch="HEAD"))

    assert events.payloads[-1]["repo"] is None
