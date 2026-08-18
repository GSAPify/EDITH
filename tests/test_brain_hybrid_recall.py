"""Brain's hybrid-recall fix (conversational-memory bug).

Root cause: Brain called the inherited graph-only ``recall()``; conv/think Facts
(spec 13's "owner asked / EDITH answered" and "think about" Facts) carry NO graph
edges, so a follow-up that doesn't literally repeat the earlier wording never came
back. ``VectorMemoryStore.recall_hybrid`` (tests/test_vector_recall.py) fuses the
graph and semantic signals; this file proves Brain (a) prefers it when the injected
store exposes it, (b) still falls back to plain ``recall`` for every fake/plain store
that doesn't, (c) uses it on BOTH the normal and the explicit "think about" path, and
(d) ``_assemble`` renders non-Fact (Repo/Project/Person/PR) hits without leaking the
hybrid fusion's private ``_recall_source``/``distance`` metadata into the prompt.

Fakes only for (a)-(c); (d) is a pure ``_assemble`` unit test; the last test is a real
``VectorMemoryStore`` + ``LocalEmbedder`` integration proving a semantic-only
conv-Fact reaches the Router through a live Brain turn.
"""

from __future__ import annotations

import pytest

from edith.brain import Brain
from edith.brain.loop import _assemble
from edith.bus import EventBus
from edith.memory.embeddings import Embedder, LocalEmbedder
from edith.memory.vector import VectorMemoryStore
from edith.router import BackgroundJob, JobStatus, ModelResponse, Tier


class FakeRouter:
    def __init__(self, answer: str = "an answer") -> None:
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


class FakeReasoner:
    def __init__(self) -> None:
        self.jobs: list[tuple[list[dict[str, object]], object]] = []

    async def think_async(
        self,
        messages: list[dict[str, object]],
        on_done,  # noqa: ANN001
        *,
        max_tokens: int = 1024,
    ) -> BackgroundJob:
        self.jobs.append((messages, on_done))
        return BackgroundJob(id="job-test", status=JobStatus.RUNNING)


class FakeHybridMemory:
    """A store exposing BOTH ``recall`` and ``recall_hybrid`` — Brain must prefer the latter."""

    def __init__(
        self,
        recall_hits: list[dict[str, object]] | None = None,
        hybrid_hits: list[dict[str, object]] | None = None,
    ) -> None:
        self.recall_hits = recall_hits or []
        self.hybrid_hits = hybrid_hits if hybrid_hits is not None else self.recall_hits
        self.recall_queries: list[str] = []
        self.hybrid_calls: list[tuple[str, int]] = []
        self.remembered_nodes: list[object] = []

    def recall(self, query: str) -> list[dict[str, object]]:
        self.recall_queries.append(query)
        return self.recall_hits

    def recall_hybrid(self, query: str, k: int = 5) -> list[dict[str, object]]:
        self.hybrid_calls.append((query, k))
        return self.hybrid_hits

    def remember(self, nodes=None, edges=None) -> None:  # noqa: ANN001
        self.remembered_nodes.extend(nodes or [])


class FakePlainMemory:
    """No ``recall_hybrid`` at all — mirrors every existing fake/plain graph-only store."""

    def __init__(self, recall_hits: list[dict[str, object]] | None = None) -> None:
        self.recall_hits = recall_hits or []
        self.recall_queries: list[str] = []
        self.remembered_nodes: list[object] = []

    def recall(self, query: str) -> list[dict[str, object]]:
        self.recall_queries.append(query)
        return self.recall_hits

    def remember(self, nodes=None, edges=None) -> None:  # noqa: ANN001
        self.remembered_nodes.extend(nodes or [])


def _blob(messages: list[dict[str, object]]) -> str:
    return " ".join(str(m.get("content", "")) for m in messages)


async def test_brain_prefers_recall_hybrid_when_present() -> None:
    bus = EventBus()
    memory = FakeHybridMemory(
        recall_hits=[{"text": "should not be used"}],
        hybrid_hits=[{"text": "from hybrid recall"}],
    )
    router = FakeRouter()
    Brain(bus=bus, memory=memory, router=router)

    await bus.publish("voice.utterance", source="voice", payload={"text": "what's up?"})

    assert memory.hybrid_calls, "recall_hybrid should have been called"
    assert memory.recall_queries == []  # plain recall must NOT be used when hybrid exists
    blob = _blob(router.calls[0][0])
    assert "from hybrid recall" in blob
    assert "should not be used" not in blob


async def test_brain_falls_back_to_recall_when_hybrid_absent() -> None:
    bus = EventBus()
    memory = FakePlainMemory(recall_hits=[{"text": "plain recall fact"}])
    router = FakeRouter()
    Brain(bus=bus, memory=memory, router=router)

    await bus.publish("voice.utterance", source="voice", payload={"text": "hello"})

    assert memory.recall_queries == ["hello"]
    assert "plain recall fact" in _blob(router.calls[0][0])


async def test_explicit_think_about_branch_also_uses_hybrid_recall() -> None:
    bus = EventBus()
    memory = FakeHybridMemory(hybrid_hits=[{"text": "hybrid context for the deep dive"}])
    router = FakeRouter()
    reasoner = FakeReasoner()
    Brain(bus=bus, memory=memory, router=router, reasoner=reasoner)

    await bus.publish(
        "voice.utterance", source="voice", payload={"text": "think about our sharding strategy"}
    )

    assert memory.hybrid_calls, "the explicit think branch must also use recall_hybrid"
    assert memory.recall_queries == []
    assert "hybrid context for the deep dive" in _blob(reasoner.jobs[0][0])


def test_assemble_includes_non_fact_hit_shapes_without_leaking_private_metadata() -> None:
    recalled = [
        {
            "label": "Repo",
            "id": "repo-1",
            "name": "widget-service",
            "summary": "handles checkout",
            "_recall_source": "graph",
        },
        {"label": "Project", "id": "proj-1", "name": "Q3 roadmap", "status": "active"},
        {"label": "Person", "id": "person-1", "name": "Alex Doe", "gh_handle": "alexdoe"},
        {
            "label": "PR",
            "id": "pr-1",
            "title": "Fix checkout bug",
            "state": "open",
            "_recall_source": "semantic",
            "distance": 0.1234,
        },
    ]

    messages = _assemble("what's the status?", recalled, "PERSONA")
    user_content = str(messages[1]["content"])

    assert "widget-service" in user_content
    assert "handles checkout" in user_content
    assert "Q3 roadmap" in user_content
    assert "active" in user_content
    assert "Alex Doe" in user_content or "alexdoe" in user_content
    assert "Fix checkout bug" in user_content
    assert "open" in user_content
    # Fusion-private metadata / raw distance must never reach the model.
    assert "_recall_source" not in user_content
    assert "distance" not in user_content
    assert "0.1234" not in user_content


@pytest.fixture(scope="module")
def embedder() -> Embedder:
    return LocalEmbedder()


async def test_conversation_like_semantic_only_fact_reaches_router_through_a_brain_turn(
    tmp_path, embedder: Embedder
) -> None:
    store = VectorMemoryStore(tmp_path / "mem.kuzu", embedder=embedder)
    bus = EventBus()
    router = FakeRouter(answer="turn one answer")
    Brain(bus=bus, memory=store, router=router)

    # Turn 1: remembered as a conv-* Fact with NO graph edges (spec 13's exact shape).
    await bus.publish(
        "voice.utterance",
        source="voice",
        payload={"text": "my favorite database migration tool is Alembic"},
    )

    # Turn 2: a semantically-related follow-up that does NOT literally repeat turn 1's
    # wording, so graph-only recall (the root cause) cannot find it — only the semantic
    # signal, layered on by recall_hybrid, can.
    await bus.publish(
        "voice.utterance",
        source="voice",
        payload={"text": "what tool do I use for database migrations?"},
    )

    assert len(router.calls) == 2
    second_messages, _tier = router.calls[1]
    assert "Alembic" in _blob(second_messages)
