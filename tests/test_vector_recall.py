"""Semantic recall over Kuzu's native HNSW vector index.

Real Kuzu VECTOR extension + a real local embedding (fastembed all-MiniLM-L6-v2,
384-dim, offline). No Bifrost, no cloud. The index is static: built after the
embeddable rows are inserted, then queried.
"""

import logging

import pytest

from edith.memory.embeddings import Embedder, LocalEmbedder
from edith.memory.store import Node
from edith.memory.vector import VectorMemoryStore


class _CountingEmbedder:
    """Wraps a real ``Embedder`` and counts ``.embed()`` calls.

    Used to prove ``recall_hybrid(k<=0)`` does no embedding work at all — a plain
    assertion on the returned hits can't distinguish "did no work" from "did the
    work and happened to return nothing".
    """

    def __init__(self, inner: Embedder) -> None:
        self._inner = inner
        self.embed_calls = 0

    @property
    def dim(self) -> int:
        return self._inner.dim

    def embed(self, text: str) -> list[float]:
        self.embed_calls += 1
        return self._inner.embed(text)


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


# --- recall_hybrid (conversational-memory fix) --------------------------------------
#
# Brain needs a fused signal: conv/think Facts (spec 13) carry no graph edges, so the
# graph-only ``recall()`` Brain used to call can miss them entirely — the root cause.
# ``recall_hybrid`` is ADDITIVE: ``recall`` and ``semantic_recall`` stay exactly as they
# are (Finder fuses them itself), this is a third, Brain-facing method.


def test_recall_hybrid_includes_graph_only_and_semantic_only_hits_in_order(
    tmp_path, embedder: Embedder
):
    store = VectorMemoryStore(tmp_path / "mem.kuzu", embedder=embedder)
    query = "why did the service account cause an error?"
    store.remember(
        nodes=[
            # Graph-only: a Repo hit. Repos are never embedded (only Facts are), so this
            # can ONLY come from the graph signal — the exact shape the root cause drops.
            Node(
                "Repo",
                "repo-onboarding-portal",
                {"name": "onboarding-portal", "summary": f"{query} — investigation notes"},
            ),
            # Semantic-only: doesn't contain the query as a literal substring, so graph
            # recall's substring scan can't find it — only the semantic signal can.
            Node("Fact", "f3", {"text": "onboarding-portal Unknown object = the service account"}),
        ]
    )

    hits = store.recall_hybrid(query, k=5)
    ids = [h["id"] for h in hits]

    assert ids[0] == "repo-onboarding-portal"  # graph/exact hits ride first
    assert hits[0]["_recall_source"] == "graph"
    assert "f3" in ids[1:]  # semantic-only hit follows
    fact_hit = next(h for h in hits if h["id"] == "f3")
    assert fact_hit["_recall_source"] == "semantic"


def test_recall_hybrid_dedupes_a_hit_present_in_both_signals(tmp_path, embedder: Embedder):
    store = VectorMemoryStore(tmp_path / "mem.kuzu", embedder=embedder)
    store.remember(
        nodes=[Node("Fact", "f1", {"text": "the deploy pipeline failed on a missing IAM role"})]
    )

    hits = store.recall_hybrid("missing IAM role", k=5)

    matches = [h for h in hits if h["id"] == "f1"]
    assert len(matches) == 1  # present in both signals, but appears exactly once
    assert matches[0]["_recall_source"] == "graph+semantic"


def test_recall_hybrid_does_not_mutate_the_source_hit_dicts(tmp_path, embedder: Embedder):
    store = VectorMemoryStore(tmp_path / "mem.kuzu", embedder=embedder)
    store.remember(
        nodes=[Node("Fact", "f1", {"text": "the deploy pipeline failed on a missing IAM role"})]
    )

    graph_hit_before = store.recall("missing IAM role")[0]
    assert "_recall_source" not in graph_hit_before

    store.recall_hybrid("missing IAM role", k=5)

    graph_hit_after = store.recall("missing IAM role")[0]
    assert "_recall_source" not in graph_hit_after  # recall()'s own hits were never tagged


def test_recall_hybrid_k_bounds_the_semantic_signal(tmp_path, embedder: Embedder):
    counting = _CountingEmbedder(embedder)
    store = VectorMemoryStore(tmp_path / "mem.kuzu", embedder=counting)
    store.remember(
        nodes=[
            Node("Fact", f"f{i}", {"text": f"widget service incident number {i} report"})
            for i in range(3)
        ]
    )
    counting.embed_calls = 0  # reset past the embeds done inside remember()

    # None of the three Facts contain the query as a literal substring (each has a
    # "number {i}" in the middle), so graph recall contributes 0 hits — every hit below
    # is purely semantic, so k=1 bounding the semantic signal to 1 is directly visible.
    hits = store.recall_hybrid("widget service incident report", k=1)

    assert len(hits) == 1
    assert counting.embed_calls == 1  # one query embedding, not one per candidate


def test_recall_hybrid_nonpositive_k_does_no_embed_or_search_work(
    tmp_path, embedder: Embedder
):
    counting = _CountingEmbedder(embedder)
    store = VectorMemoryStore(tmp_path / "mem.kuzu", embedder=counting)
    store.remember(nodes=[Node("Fact", "f1", {"text": "widget service incident report"})])
    counting.embed_calls = 0

    assert store.recall_hybrid("widget service incident report", k=0) == []
    assert store.recall_hybrid("widget service incident report", k=-3) == []
    assert counting.embed_calls == 0


def test_recall_hybrid_debug_is_off_by_default(tmp_path, embedder: Embedder, monkeypatch, caplog):
    monkeypatch.delenv("EDITH_MEMORY_DEBUG", raising=False)
    store = VectorMemoryStore(tmp_path / "mem.kuzu", embedder=embedder)
    store.remember(nodes=[Node("Fact", "f1", {"text": "quiet by default"})])

    with caplog.at_level(logging.DEBUG, logger="edith.memory.vector"):
        store.recall_hybrid("quiet", k=3)

    assert caplog.text == ""


def test_recall_hybrid_debug_logs_only_counts_and_distances_never_ids(
    tmp_path, embedder: Embedder, monkeypatch, caplog
):
    # round 4 review: ids are owner-derived and not guaranteed secret-free —
    # sanitize_node sanitizes only properties, while ingestion derives ids directly
    # from person/project names (edith/ingest/graph_map.py:41-48). A secret-shaped id
    # must never reach the debug log; only counts + numeric distances may.
    monkeypatch.setenv("EDITH_MEMORY_DEBUG", "1")
    marker = "zz-do-not-log-this-fact-text-9182"
    secret_id = "GOCSPX-EXAMPLE_FAKE_SECRET_ID_DO_NOT_LOG"
    store = VectorMemoryStore(tmp_path / "mem.kuzu", embedder=embedder)
    store.remember(nodes=[Node("Fact", secret_id, {"text": f"onboarding notes: {marker}"})])

    with caplog.at_level(logging.DEBUG, logger="edith.memory.vector"):
        hits = store.recall_hybrid("onboarding notes", k=3)

    assert any(h["id"] == secret_id for h in hits)
    assert marker not in caplog.text  # recalled TEXT never reaches the debug log
    assert secret_id not in caplog.text  # nor does the (possibly secret-shaped) id
    assert "total=" in caplog.text
    assert "graph=" in caplog.text
    assert "semantic=" in caplog.text
