"""Semantic recall over Kuzu's native HNSW vector index.

Real Kuzu VECTOR extension + a real local embedding (fastembed all-MiniLM-L6-v2,
384-dim, offline). No Bifrost, no cloud. The index is static: built after the
embeddable rows are inserted, then queried.
"""

import pytest

from edith.memory.embeddings import Embedder, LocalEmbedder
from edith.memory.store import Node
from edith.memory.vector import VectorMemoryStore


@pytest.fixture(scope="module")
def embedder() -> Embedder:
    # Module-scoped: loading the ONNX model once keeps the suite fast.
    return LocalEmbedder()


def test_local_embedder_is_384_dim(embedder: Embedder):
    vec = embedder.embed("the onboarding-portal service account")
    assert len(vec) == embedder.dim == 384
    assert all(isinstance(x, float) for x in vec)


def test_semantic_recall_finds_related_fact(tmp_path, embedder: Embedder):
    store = VectorMemoryStore(tmp_path / "mem.kuzu", embedder=embedder)
    store.remember(
        nodes=[
            Node("Fact", "f1", {"text": "the deploy pipeline failed on a missing IAM role"}),
            Node("Fact", "f2", {"text": "coffee machine on the third floor is broken"}),
            Node("Fact", "f3", {"text": "onboarding-portal Unknown object = the service account"}),
        ]
    )
    # Static HNSW: build the index once the rows exist.
    store.build_vector_index()

    hits = store.semantic_recall("why did the service account cause an error?", k=2)
    ids = [h["id"] for h in hits]
    # The service-account fact should rank in the top-2; coffee should not lead.
    assert "f3" in ids
    assert ids[0] != "f2"


def test_semantic_recall_empty_before_index_build(tmp_path, embedder: Embedder):
    store = VectorMemoryStore(tmp_path / "mem.kuzu", embedder=embedder)
    store.remember(nodes=[Node("Fact", "f1", {"text": "a fact with no index yet"})])
    # No rebuild called: semantic recall returns nothing (no index), but the
    # structural graph recall still finds it — the design-around for static HNSW.
    assert store.semantic_recall("fact", k=5) == []
    assert any(h["id"] == "f1" for h in store.recall("fact"))


def test_semantic_recall_after_reopen(tmp_path, embedder: Embedder):
    # The persisted HNSW index is directly queryable after a fresh reopen with
    # NO rebuild (verified against Kuzu 0.11.3). This is the semantic half of the
    # recall-across-restart promise: a daemon that goes down and comes back keeps
    # its vector recall without re-indexing.
    db_path = tmp_path / "mem.kuzu"
    store = VectorMemoryStore(db_path, embedder=embedder)
    store.remember(nodes=[Node("Fact", "f1", {"text": "the CI runner ran out of disk"})])
    store.build_vector_index()
    store.close()

    reopened = VectorMemoryStore(db_path, embedder=embedder)
    ids = [h["id"] for h in reopened.semantic_recall("disk space on the CI machine", k=1)]
    assert ids == ["f1"]
