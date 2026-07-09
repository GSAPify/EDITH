"""Narrator (spec 04 §Step 3): the three-class narration policy over session events.

Silent (routine tool calls) · Spoken-local (start/stop/error/idle, template, no model) ·
Model-gated (rare, budget-checked haiku summary). Idle is a timer, not a record.
"""

from __future__ import annotations

import pytest

from edith.bus import EventBus
from edith.router import ModelResponse, Tier
from edith.session.narrator import Narrator

pytestmark = pytest.mark.asyncio


class _Speaker:
    def __init__(self) -> None:
        self.said: list[str] = []

    async def __call__(self, text: str) -> None:
        self.said.append(text)


class _FakeRouter:
    def __init__(self) -> None:
        self.calls: list[tuple[list, Tier]] = []

    async def model_call(self, messages, tier_hint, max_tokens=512) -> ModelResponse:
        self.calls.append((messages, tier_hint))
        return ModelResponse(
            text="Session 2 hit a database error and is retrying.",
            input_tokens=0,
            output_tokens=0,
        )


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


async def _event(bus: EventBus, **payload) -> None:
    await bus.publish("session.event", source="session_bus", payload=payload)


def _mk(bus, speak, **kw) -> Narrator:
    return Narrator(bus, speak, **kw)


async def test_start_is_spoken_locally_without_a_model() -> None:
    bus = EventBus()
    spk = _Speaker()
    router = _FakeRouter()
    _mk(bus, spk, router=router)
    await _event(bus, session_id="s1", kind="start", summary="started in agents", repo="agents")
    assert len(spk.said) == 1
    assert "agents" in spk.said[0]
    assert router.calls == []  # local template — no model call


async def test_tool_use_is_silent() -> None:
    bus = EventBus()
    spk = _Speaker()
    _mk(bus, spk)
    await _event(bus, session_id="s1", kind="tool_use", summary="using Bash", repo="agents")
    await _event(bus, session_id="s1", kind="prompt", summary="owner: do x", repo="agents")
    assert spk.said == []


async def test_error_without_router_uses_local_template() -> None:
    bus = EventBus()
    spk = _Speaker()
    _mk(bus, spk)  # no router
    await _event(bus, session_id="s1", kind="error", summary="a tool call failed", repo="agents")
    assert len(spk.said) == 1
    assert "agents" in spk.said[0]


async def test_error_with_router_and_budget_uses_model() -> None:
    bus = EventBus()
    spk = _Speaker()
    router = _FakeRouter()
    _mk(bus, spk, router=router, budget_gate=lambda: True)
    await _event(bus, session_id="s2", kind="error", summary="connection refused", repo="agents")
    assert len(router.calls) == 1
    assert router.calls[0][1] is Tier.HAIKU  # narration stays cheap
    assert spk.said == ["Session 2 hit a database error and is retrying."]


async def test_error_model_gated_denied_by_budget_falls_back_local() -> None:
    bus = EventBus()
    spk = _Speaker()
    router = _FakeRouter()
    _mk(bus, spk, router=router, budget_gate=lambda: False)
    await _event(bus, session_id="s2", kind="error", summary="boom", repo="agents")
    assert router.calls == []           # budget denied → no model call
    assert len(spk.said) == 1           # still narrated, locally


async def test_idle_is_spoken_once_after_the_threshold() -> None:
    bus = EventBus()
    spk = _Speaker()
    clock = _Clock()
    n = _mk(bus, spk, clock=clock, idle_seconds=120)
    await _event(bus, session_id="s1", kind="tool_use", summary="using Bash", repo="agents")
    assert spk.said == []  # tool_use silent

    clock.t += 130
    await n.tick()
    assert len(spk.said) == 1 and "waiting" in spk.said[0].lower()

    clock.t += 130
    await n.tick()
    assert len(spk.said) == 1  # not repeated while still idle


async def test_new_activity_resets_idle() -> None:
    bus = EventBus()
    spk = _Speaker()
    clock = _Clock()
    n = _mk(bus, spk, clock=clock, idle_seconds=120)
    await _event(bus, session_id="s1", kind="tool_use", summary="using Bash", repo="agents")
    clock.t += 130
    await n.tick()
    assert len(spk.said) == 1  # first idle narration

    # Fresh activity, then idle again → narrated again.
    await _event(bus, session_id="s1", kind="tool_use", summary="using Grep", repo="agents")
    clock.t += 130
    await n.tick()
    assert len(spk.said) == 2
