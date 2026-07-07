"""Brain resolve-on-miss hook (spec 09 §Brain wiring).

When an utterance references a repo that recall MISSES, Brain calls the injected
``resolve_repo``-shaped resolver BEFORE answering, folds its fast answer into the
working context, and (for a RESOLVED result) schedules the background deep-extract
via ``asyncio.create_task`` — never blocking the answer.

The resolver is injected and defaults to ``None`` (no-op) so the existing Brain
tests — which already run with EMPTY recall (a "miss") — behave exactly as before.
Tested with a fake resolver + fake Router + fake Memory + real bus; no live model.
"""

from __future__ import annotations

from edith.brain import Brain
from edith.bus import Event, EventBus
from edith.finder import ResolveResult, ResolveStatus
from edith.router import ModelResponse, Tier


class FakeRouter:
    def __init__(self, answer: str = "done") -> None:
        self.answer = answer
        self.calls: list[tuple[list[dict[str, object]], Tier]] = []

    async def model_call(
        self, messages: list[dict[str, object]], tier_hint: Tier, max_tokens: int = 1024
    ) -> ModelResponse:
        self.calls.append((messages, tier_hint))
        return ModelResponse(text=self.answer, input_tokens=1, output_tokens=1)


class FakeMemory:
    def __init__(self, recall_hits: list[dict[str, object]] | None = None) -> None:
        self.recall_hits = recall_hits or []
        self.remembered_nodes: list[object] = []

    def recall(self, query: str) -> list[dict[str, object]]:
        return self.recall_hits

    def remember(self, nodes=None, edges=None) -> None:  # noqa: ANN001
        self.remembered_nodes.extend(nodes or [])


class FakeResolver:
    def __init__(self, result: ResolveResult) -> None:
        self.result = result
        self.names: list[str] = []

    async def __call__(self, name: str) -> ResolveResult:
        self.names.append(name)
        return self.result


async def _run_utterance(bus: EventBus, text: str) -> list[Event]:
    decisions: list[Event] = []

    async def capture(event: Event) -> None:
        decisions.append(event)

    bus.subscribe("brain.decision", capture)
    await bus.publish("voice.utterance", source="voice", payload={"text": text})
    return decisions


async def test_unknown_repo_utterance_invokes_resolver_and_answers() -> None:
    bus = EventBus()
    memory = FakeMemory(recall_hits=[])  # a MISS
    router = FakeRouter(answer="Here's what I found.")
    resolver = FakeResolver(
        ResolveResult(ResolveStatus.RESOLVED, name="widget",
                      answer="widget is a service.", background=None)
    )
    Brain(bus=bus, memory=memory, router=router, resolve_repo=resolver)

    decisions = await _run_utterance(bus, "what is the widget repo?")

    assert resolver.names == ["widget"]           # resolver invoked on the miss
    assert len(decisions) == 1                     # an answer was produced
    assert decisions[0].payload["action"] == "answer"
    # the fast resolve answer was folded into the model's working context
    blob = " ".join(str(m.get("content", "")) for m in router.calls[0][0])
    assert "widget is a service." in blob


async def test_resolver_not_called_when_recall_hits() -> None:
    bus = EventBus()
    memory = FakeMemory(recall_hits=[{"label": "Repo", "id": "repo-widget", "text": "widget"}])
    router = FakeRouter()
    resolver = FakeResolver(
        ResolveResult(ResolveStatus.NOT_FOUND, name="", answer="", background=None)
    )
    Brain(bus=bus, memory=memory, router=router, resolve_repo=resolver)

    await _run_utterance(bus, "how is the widget repo?")

    assert resolver.names == []  # recall hit -> no resolve needed


async def test_no_resolver_by_default_behaves_as_before() -> None:
    """Default resolver is None: a recall miss proceeds straight to the model,
    exactly as the pre-hook Brain (keeps the existing 96 tests green)."""
    bus = EventBus()
    memory = FakeMemory(recall_hits=[])  # miss, but no resolver injected
    router = FakeRouter(answer="ok")
    Brain(bus=bus, memory=memory, router=router)  # no resolve_repo arg

    decisions = await _run_utterance(bus, "just a plain question")

    assert len(router.calls) == 1
    assert len(decisions) == 1
    assert decisions[0].payload["answer"] == "ok"
