"""Brain pause semantics (spec 01 §"Pause + Memory — explicit decision").

When the daemon is paused, a ``voice.utterance`` must NOT trigger a model call
AND must NOT be remembered — the privacy-respecting reading: a manual pause
means "don't capture this moment." The in-RAM conversation buffer up to the
pause is retained (Brain is stateless per-utterance in this slice, so "retain"
is satisfied by simply not dropping anything — no buffer object is invented).

Brain reads ``is_paused`` (a zero-arg predicate wired from the daemon's
RuntimeState). Default is not-paused so the existing Brain tests are unchanged.

Asserted:
  - paused  ⇒ router.calls == [] AND memory.remembered_nodes == [] (no publish),
  - running ⇒ both a model call and a remember happen (regression guard).
"""

from __future__ import annotations

from edith.brain import Brain
from edith.bus import Event, EventBus
from edith.router import ModelResponse, Tier


class FakeRouter:
    def __init__(self) -> None:
        self.calls: list[tuple[list[dict[str, object]], Tier]] = []

    async def model_call(
        self,
        messages: list[dict[str, object]],
        tier_hint: Tier,
        max_tokens: int = 1024,
    ) -> ModelResponse:
        self.calls.append((messages, tier_hint))
        return ModelResponse(text="answer", input_tokens=1, output_tokens=1)


class SpyMemory:
    def __init__(self) -> None:
        self.recall_queries: list[str] = []
        self.remembered_nodes: list[object] = []

    def recall(self, query: str) -> list[dict[str, object]]:
        self.recall_queries.append(query)
        return []

    def remember(self, nodes=None, edges=None) -> None:  # noqa: ANN001
        self.remembered_nodes.extend(nodes or [])


async def _utter(bus: EventBus, text: str) -> list[Event]:
    decisions: list[Event] = []

    async def capture(event: Event) -> None:
        decisions.append(event)

    bus.subscribe("brain.decision", capture)
    await bus.publish("voice.utterance", source="voice", payload={"text": text})
    return decisions


async def test_paused_skips_model_call_and_remember():
    bus = EventBus()
    memory = SpyMemory()
    router = FakeRouter()
    Brain(bus=bus, memory=memory, router=router, is_paused=lambda: True)

    decisions = await _utter(bus, text="what's the deploy status?")

    assert router.calls == []  # no model call while paused
    assert memory.remembered_nodes == []  # nothing captured while paused
    assert decisions == []  # no decision published


async def test_running_does_model_call_and_remember():
    # Regression guard for the not-paused path (mirrors the default Brain tests).
    bus = EventBus()
    memory = SpyMemory()
    router = FakeRouter()
    Brain(bus=bus, memory=memory, router=router, is_paused=lambda: False)

    decisions = await _utter(bus, text="what's the deploy status?")

    assert len(router.calls) == 1
    assert len(memory.remembered_nodes) == 1
    assert len(decisions) == 1


async def test_default_is_not_paused():
    # No is_paused argument -> behaves exactly as before (keeps existing tests green).
    bus = EventBus()
    memory = SpyMemory()
    router = FakeRouter()
    Brain(bus=bus, memory=memory, router=router)

    await _utter(bus, text="hello")

    assert len(router.calls) == 1
    assert len(memory.remembered_nodes) == 1
