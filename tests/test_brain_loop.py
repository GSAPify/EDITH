"""Brain loop (spec 01 §"The core loop" / §"Brain / Orchestrator loop").

On a ``voice.utterance`` event Brain runs one pass:
recall -> assemble working context -> redact -> Router.model_call ->
remember the exchange -> publish ``brain.decision``.

Tested with an injected fake Router (deterministic canned response), a fake
Memory (records recall/remember calls), and the real bus — so the loop logic is
proven without any live model call. Assertions:
  - Memory.recall was consulted with the utterance,
  - redaction ran BEFORE the model call (a planted secret never reaches Router),
  - the exchange (utterance + answer) was remembered,
  - a ``brain.decision`` event was published.
"""

from __future__ import annotations

from edith.brain import Brain
from edith.bus import Event, EventBus
from edith.router import ModelResponse, Tier


class FakeRouter:
    def __init__(self, answer: str = "check the service account first") -> None:
        self.answer = answer
        self.calls: list[tuple[list[dict[str, object]], Tier]] = []

    async def model_call(
        self,
        messages: list[dict[str, object]],
        tier_hint: Tier,
        max_tokens: int = 1024,
    ) -> ModelResponse:
        self.calls.append((messages, tier_hint))
        return ModelResponse(text=self.answer, input_tokens=5, output_tokens=4)


class FakeMemory:
    def __init__(self, recall_hits: list[dict[str, object]] | None = None) -> None:
        self.recall_hits = recall_hits or []
        self.recall_queries: list[str] = []
        self.remembered_nodes: list[object] = []

    def recall(self, query: str) -> list[dict[str, object]]:
        self.recall_queries.append(query)
        return self.recall_hits

    def remember(self, nodes=None, edges=None) -> None:  # noqa: ANN001
        self.remembered_nodes.extend(nodes or [])


async def _run_utterance(bus: EventBus, text: str) -> list[Event]:
    """Publish an utterance and collect any brain.decision events it produces.

    Brain subscribes itself to the bus on construction, so the caller only needs
    the bus. Delivery is synchronous (bus.publish awaits all handlers), so the
    decisions list is complete when this returns.
    """
    decisions: list[Event] = []

    async def capture(event: Event) -> None:
        decisions.append(event)

    bus.subscribe("brain.decision", capture)
    await bus.publish("voice.utterance", source="voice", payload={"text": text})
    return decisions


async def test_recall_is_consulted_with_the_utterance():
    bus = EventBus()
    memory = FakeMemory()
    router = FakeRouter()
    Brain(bus=bus, memory=memory, router=router)

    await _run_utterance(bus=bus, text="why is onboarding-portal broken?")

    assert memory.recall_queries == ["why is onboarding-portal broken?"]


async def test_model_call_made_and_decision_published():
    bus = EventBus()
    memory = FakeMemory(
        recall_hits=[{"label": "Fact", "id": "f1", "text": "SA not shared on template"}]
    )
    router = FakeRouter(answer="It was the service account not shared on the template.")
    Brain(bus=bus, memory=memory, router=router)

    decisions = await _run_utterance(
        bus=bus, text="onboarding-portal Unknown object again?"
    )

    assert len(router.calls) == 1
    # the recalled fact was assembled into the working context sent to the model
    messages, _tier = router.calls[0]
    blob = " ".join(str(m.get("content", "")) for m in messages)
    assert "SA not shared on template" in blob
    assert "onboarding-portal Unknown object again?" in blob

    assert len(decisions) == 1
    assert decisions[0].topic == "brain.decision"
    assert decisions[0].payload["action"] == "answer"


async def test_exchange_is_remembered():
    bus = EventBus()
    memory = FakeMemory()
    router = FakeRouter(answer="Yes.")
    Brain(bus=bus, memory=memory, router=router)

    await _run_utterance(bus=bus, text="is the deploy done?")

    remembered_text = " ".join(
        str(getattr(n, "props", {}).get("text", "")) for n in memory.remembered_nodes
    )
    assert "is the deploy done?" in remembered_text
    assert "Yes." in remembered_text


async def test_redaction_runs_before_the_model_call():
    bus = EventBus()
    memory = FakeMemory()
    router = FakeRouter()
    Brain(bus=bus, memory=memory, router=router)

    secret = "GOCSPX-EXAMPLE_FAKE_SECRET_DO_NOT_STORE"
    await _run_utterance(
        bus=bus, text=f"my client_secret: {secret} — remember it"
    )

    messages, _tier = router.calls[0]
    blob = " ".join(str(m.get("content", "")) for m in messages)
    assert secret not in blob  # redacted before ever reaching the model
    assert "[REDACTED]" in blob

    # and the secret is likewise absent from what was remembered
    remembered_text = " ".join(
        str(getattr(n, "props", {}).get("text", "")) for n in memory.remembered_nodes
    )
    assert secret not in remembered_text
