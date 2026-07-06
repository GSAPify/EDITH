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


def test_semantic_recall_works_without_build_step(tmp_path, embedder: Embedder):
    # DELIBERATE DEVIATION from the old build-once contract. The prior test here
    # (`_empty_before_index_build`) asserted `semantic_recall(...) == []` before
    # a build — that == [] *was* the Kuzu build-once limitation this refactor
    # removes. With sqlite-vec inserts are incremental, so a remembered Fact is
    # semantically searchable immediately, with no build step. Graph recall still
    # finds it too (both signals stay independently correct).
    store = VectorMemoryStore(tmp_path / "mem.kuzu", embedder=embedder)
    store.remember(nodes=[Node("Fact", "f1", {"text": "a fact with no build step"})])
    assert any(h["id"] == "f1" for h in store.semantic_recall("fact", k=5))
    assert any(h["id"] == "f1" for h in store.recall("fact"))


def test_secret_never_reaches_the_vector_store(tmp_path, embedder: Embedder):
    # New persistence surface (the sqlite-vec file) must honour the same
    # never-persist guarantee as the graph: sanitize runs FIRST in remember,
    # so a credential-shaped fact is redacted before it is embedded or stored.
    fake_secret = "GOCSPX-EXAMPLE_FAKE_SECRET_DO_NOT_STORE"
    store = VectorMemoryStore(tmp_path / "mem.kuzu", embedder=embedder)
    store.remember(nodes=[Node("Fact", "f1", {"text": f"owner client_secret: {fake_secret}"})])

    stored_text = [row[0] for row in store._vec.execute("SELECT text FROM fact_map").fetchall()]
    assert stored_text, "the sanitized fact should still be stored"
    assert all(fake_secret not in t for t in stored_text)
    assert any("[REDACTED]" in t for t in stored_text)


def test_fact_remembered_after_index_exists_is_recalled_immediately(tmp_path, embedder: Embedder):
    # THE defining capability Kuzu's build-once HNSW lacked: a Fact remembered
    # AFTER the index/store already exists must be immediately returned by
    # semantic recall, with NO rebuild. Written RED first (fails on build-once
    # Kuzu: f2 lands in the table but never enters the already-built index);
    # green with sqlite-vec's incremental inserts.
    store = VectorMemoryStore(tmp_path / "mem.kuzu", embedder=embedder)
    store.remember(nodes=[Node("Fact", "f1", {"text": "the deploy pipeline failed on IAM"})])
    store.build_vector_index()  # index now exists

    # A brand-new fact, written after the index already exists.
    store.remember(
        nodes=[Node("Fact", "f2", {"text": "the staging database ran out of connections"})]
    )

    hits = store.semantic_recall("why did staging run out of db connections?", k=1)
    assert [h["id"] for h in hits] == ["f2"]


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
