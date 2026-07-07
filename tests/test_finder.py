"""NL repo finder (spec 09 §NL finder).

``find_repos(query, k)`` does semantic + graph search over the ingested Memory
graph: embed/recall query -> vector-similar & substring-matched Facts/Repos ->
walk ``relates_to`` edges to Repo nodes -> rank by similarity + degree.

Tested against a temp ``VectorMemoryStore`` seeded with a few Repo+Fact nodes
so the relevant repo ranks first WITHOUT any model call — the ranking is
deterministic and model-free. The Sonnet summary is a separate, injected path
(fake Router) and never affects the ranking.
"""

from __future__ import annotations

from pathlib import Path

from edith.finder import RepoHit, find_repos, summarize_hits
from edith.memory.store import Edge, Node
from edith.memory.vector import VectorMemoryStore
from edith.router import ModelResponse, Tier


def _seed(store: VectorMemoryStore) -> None:
    """Three repos with distinguishing Facts related to each."""
    store.remember(
        nodes=[
            Node("Repo", "repo-approvals", {"name": "approvals", "summary": "approval svc"}),
            Node("Repo", "repo-catalyst", {"name": "catalyst", "summary": "data catalog"}),
            Node("Repo", "repo-devi", {"name": "devi", "summary": "developer tooling"}),
            Node("Fact", "fact-a",
                 {"text": "approvals handles seller approval and send", "source": "readme"}),
            Node("Fact", "fact-b",
                 {"text": "catalyst indexes the product catalog", "source": "readme"}),
            Node("Fact", "fact-c",
                 {"text": "devi is a CLI for dev environment setup", "source": "readme"}),
        ],
        edges=[
            Edge("relates_to", "Fact", "fact-a", "Repo", "repo-approvals"),
            Edge("relates_to", "Fact", "fact-b", "Repo", "repo-catalyst"),
            Edge("relates_to", "Fact", "fact-c", "Repo", "repo-devi"),
        ],
    )


class FakeRouter:
    def __init__(self) -> None:
        self.calls: list[tuple[list[dict[str, object]], Tier]] = []

    async def model_call(
        self, messages: list[dict[str, object]], tier_hint: Tier, max_tokens: int = 1024
    ) -> ModelResponse:
        self.calls.append((messages, tier_hint))
        return ModelResponse(
            text="The approvals repo is the seller approval workflow.",
            input_tokens=1, output_tokens=1,
        )


def test_relevant_repo_ranked_first_no_model_call(tmp_path: Path) -> None:
    store = VectorMemoryStore(tmp_path / "memory.kuzu")
    try:
        _seed(store)
        hits = find_repos("which repo handles the seller approval workflow?", store, k=3)
    finally:
        store.close()

    assert hits, "expected at least one repo hit"
    assert isinstance(hits[0], RepoHit)
    assert hits[0].name == "approvals"
    # ranking is model-free — no Router needed at all for find_repos
    assert all(isinstance(h, RepoHit) for h in hits)


def test_find_repos_respects_k(tmp_path: Path) -> None:
    store = VectorMemoryStore(tmp_path / "memory.kuzu")
    try:
        _seed(store)
        hits = find_repos("developer tooling and catalog", store, k=2)
    finally:
        store.close()
    assert len(hits) <= 2


def test_graph_only_fallback_when_no_vectors(tmp_path: Path) -> None:
    """A plain (non-vector) store still finds repos via substring graph recall."""
    from edith.memory.store import MemoryStore

    store = MemoryStore(tmp_path / "memory.kuzu")
    try:
        store.remember(
            nodes=[
                Node("Repo", "repo-approvals", {"name": "approvals", "summary": "approval svc"}),
                Node("Fact", "fact-a",
                     {"text": "approvals handles the seller approval flow", "source": "readme"}),
            ],
            edges=[Edge("relates_to", "Fact", "fact-a", "Repo", "repo-approvals")],
        )
        hits = find_repos("seller approval", store, k=5)
    finally:
        store.close()
    assert hits
    assert hits[0].name == "approvals"


def test_graph_token_fallback_when_no_verbatim_substring(tmp_path: Path) -> None:
    """Fix 3: a multi-word NL query whose tokens appear in the graph but NOT as a
    verbatim phrase must still return the repo, via a per-token graph fallback.
    ``store.recall`` scans the whole query as one substring, so "seo tools"
    misses even though "seo" and "tools" each match — RED before the fallback."""
    from edith.memory.store import MemoryStore

    store = MemoryStore(tmp_path / "memory.kuzu")
    try:
        store.remember(
            nodes=[
                Node("Repo", "repo-seo", {"name": "seotron", "summary": "seo tooling"}),
                Node("Fact", "fact-s",
                     {"text": "seotron provides tools for keyword research", "source": "readme"}),
            ],
            edges=[Edge("relates_to", "Fact", "fact-s", "Repo", "repo-seo")],
        )
        # No verbatim "seo tools" substring anywhere; tokens "seo"+"tools" do match.
        hits = find_repos("seo tools", store, k=5)
    finally:
        store.close()
    assert hits, "token fallback must return the repo when no verbatim phrase matches"
    assert hits[0].name == "seotron"


async def test_summarize_hits_uses_sonnet(tmp_path: Path) -> None:
    router = FakeRouter()
    hits = [
        RepoHit(name="approvals", repo_id="repo-approvals", summary="approval workflow",
                score=1.0, degree=1),
    ]
    answer = await summarize_hits("what handles approvals?", hits, router)

    assert "approvals" in answer.lower()
    assert len(router.calls) == 1
    _messages, tier = router.calls[0]
    assert tier is Tier.SONNET


async def test_summarize_hits_empty_no_model_call() -> None:
    router = FakeRouter()
    answer = await summarize_hits("nothing", [], router)
    assert router.calls == []
    assert answer  # a plain "no repos found" style message
