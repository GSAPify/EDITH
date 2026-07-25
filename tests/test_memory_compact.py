"""Tests for MemoryStore.compact() and VectorMemoryStore.compact().

All tests use a temp-dir store (headless, no network, no model calls).
The ``learned_at`` values are seeded as realistic Unix-timestamp strings
(str(1_720_000_000.0 + i)) so that lexicographic ordering matches recency
ordering — matching production writes (str(time.time())).
"""

from __future__ import annotations

import pytest

from edith.memory.embeddings import LocalEmbedder
from edith.memory.store import Edge, MemoryStore, Node
from edith.memory.vector import VectorMemoryStore

# Realistic base timestamp; adding integer offsets keeps sort order unambiguous.
_BASE_TS = 1_720_000_000.0


def _conv_node(i: int) -> Node:
    """Return a conversation Fact with a realistic ``learned_at`` string."""
    ts = str(_BASE_TS + i)
    return Node(
        label="Fact",
        id=f"conv-{ts}",
        props={"text": f"turn {i}", "learned_at": ts, "source": "brain"},
    )


def _repo_node(i: int) -> Node:
    """Return a non-conversation Fact (should never be evicted)."""
    ts = str(_BASE_TS + i)
    return Node(
        label="Fact",
        id=f"repo-{i}",
        props={"text": f"repo fact {i}", "learned_at": ts, "source": "ingest"},
    )


# ---------------------------------------------------------------------------
# MemoryStore (graph-only) tests
# ---------------------------------------------------------------------------


class TestMemoryStoreCompact:
    def test_compact_evicts_oldest_keeps_newest(self, tmp_path: pytest.TempPathFactory) -> None:
        """Seeding 10 conv Facts with max=5 must keep the 5 newest, evict the 5 oldest."""
        store = MemoryStore(tmp_path / "mem.kuzu")
        for i in range(10):
            store.remember(nodes=[_conv_node(i)])

        evicted = store.compact(max_conversation_facts=5)

        assert evicted == 5, f"expected 5 evicted, got {evicted}"
        # Verify the 5 NEWEST survive (indices 5-9).
        surviving_ids = _fact_ids(store)
        for i in range(5, 10):
            ts = str(_BASE_TS + i)
            assert f"conv-{ts}" in surviving_ids, f"newest conv-{ts} was wrongly evicted"
        # Verify the 5 OLDEST are gone (indices 0-4).
        for i in range(5):
            ts = str(_BASE_TS + i)
            assert f"conv-{ts}" not in surviving_ids, f"oldest conv-{ts} was wrongly kept"

    def test_non_conversation_facts_never_evicted(self, tmp_path: pytest.TempPathFactory) -> None:
        """repo-* and other non-conv Facts must survive regardless of count."""
        store = MemoryStore(tmp_path / "mem.kuzu")
        # Seed 10 conv Facts and 3 non-conv Facts.
        for i in range(10):
            store.remember(nodes=[_conv_node(i)])
        for i in range(3):
            store.remember(nodes=[_repo_node(i)])

        store.compact(max_conversation_facts=5)

        surviving_ids = _fact_ids(store)
        for i in range(3):
            assert f"repo-{i}" in surviving_ids, f"non-conv repo-{i} was wrongly evicted"

    def test_compact_with_edge_on_evictable_fact(self, tmp_path: pytest.TempPathFactory) -> None:
        """An evictable Fact WITH a relates_to edge must be deleted without error.

        Uses DETACH DELETE to also remove the edge atomically.
        """
        store = MemoryStore(tmp_path / "mem.kuzu")
        store.remember(
            nodes=[
                Node("Project", "proj-1", {"name": "edith", "status": "active"}),
            ]
        )
        # Seed 6 conv Facts; attach an edge on the oldest one.
        for i in range(6):
            store.remember(nodes=[_conv_node(i)])
        # Add a relates_to edge from the oldest conv Fact to the project.
        oldest_ts = str(_BASE_TS + 0)
        store.remember(
            edges=[Edge("relates_to", "Fact", f"conv-{oldest_ts}", "Project", "proj-1")]
        )

        # compact with max=5 should evict 1 (the oldest, which has an edge).
        evicted = store.compact(max_conversation_facts=5)

        assert evicted == 1
        surviving_ids = _fact_ids(store)
        assert f"conv-{oldest_ts}" not in surviving_ids

    def test_compact_idempotent(self, tmp_path: pytest.TempPathFactory) -> None:
        """A second compact() with the same limit must evict 0 additional nodes."""
        store = MemoryStore(tmp_path / "mem.kuzu")
        for i in range(8):
            store.remember(nodes=[_conv_node(i)])

        first = store.compact(max_conversation_facts=5)
        assert first == 3

        second = store.compact(max_conversation_facts=5)
        assert second == 0, f"second compact() should evict 0, got {second}"

    def test_compact_under_limit_evicts_nothing(self, tmp_path: pytest.TempPathFactory) -> None:
        """When conv Fact count is already <= max, evicted count must be 0."""
        store = MemoryStore(tmp_path / "mem.kuzu")
        for i in range(3):
            store.remember(nodes=[_conv_node(i)])

        evicted = store.compact(max_conversation_facts=10)

        assert evicted == 0
        assert store.count("Fact") == 3

    def test_compact_returns_evicted_count(self, tmp_path: pytest.TempPathFactory) -> None:
        """Return value must equal number of nodes deleted."""
        store = MemoryStore(tmp_path / "mem.kuzu")
        for i in range(20):
            store.remember(nodes=[_conv_node(i)])

        evicted = store.compact(max_conversation_facts=15)
        assert evicted == 5


# ---------------------------------------------------------------------------
# VectorMemoryStore tests — also check for orphaned embeddings
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def embedder() -> LocalEmbedder:
    return LocalEmbedder()


class TestVectorMemoryStoreCompact:
    def test_compact_evicts_oldest_and_removes_embeddings(
        self, tmp_path: pytest.TempPathFactory, embedder: LocalEmbedder
    ) -> None:
        """Evicted conv Facts must have their rows removed from BOTH fact_vectors and fact_map."""
        store = VectorMemoryStore(tmp_path / "mem.kuzu", embedder=embedder)
        for i in range(8):
            store.remember(nodes=[_conv_node(i)])

        evicted = store.compact(max_conversation_facts=5)
        assert evicted == 3

        surviving_ids = _fact_ids(store)
        for i in range(5, 8):
            ts = str(_BASE_TS + i)
            assert f"conv-{ts}" in surviving_ids, f"newest conv-{ts} wrongly evicted"
        for i in range(3):
            ts = str(_BASE_TS + i)
            assert f"conv-{ts}" not in surviving_ids, f"oldest conv-{ts} wrongly kept"

        # No orphaned rows in either sqlite-vec table.
        _assert_no_orphans(store)

    def test_no_orphaned_fact_vectors_after_compact(
        self, tmp_path: pytest.TempPathFactory, embedder: LocalEmbedder
    ) -> None:
        """fact_vectors and fact_map must contain NO rows for evicted Fact ids."""
        store = VectorMemoryStore(tmp_path / "mem.kuzu", embedder=embedder)
        for i in range(10):
            store.remember(nodes=[_conv_node(i)])

        evicted_ids: set[str] = set()
        for i in range(5):
            evicted_ids.add(f"conv-{str(_BASE_TS + i)}")

        store.compact(max_conversation_facts=5)

        # None of the evicted ids should remain in fact_map.
        remaining_vec_ids = {
            row[0]
            for row in store._vec.execute("SELECT fact_id FROM fact_map").fetchall()
        }
        for evicted_id in evicted_ids:
            assert evicted_id not in remaining_vec_ids, (
                f"orphaned embedding remains in fact_map for evicted {evicted_id}"
            )

        # fact_vectors count must match fact_map count (referential integrity).
        map_count = store._vec.execute("SELECT COUNT(*) FROM fact_map").fetchone()[0]
        vec_count = store._vec.execute(
            "SELECT COUNT(*) FROM fact_vectors"
        ).fetchone()[0]
        assert map_count == vec_count, (
            f"fact_map has {map_count} rows but fact_vectors has {vec_count} rows — orphan"
        )

    def test_compact_idempotent_no_orphans(
        self, tmp_path: pytest.TempPathFactory, embedder: LocalEmbedder
    ) -> None:
        """Running compact() twice must leave zero orphans and evict 0 on the second call."""
        store = VectorMemoryStore(tmp_path / "mem.kuzu", embedder=embedder)
        for i in range(7):
            store.remember(nodes=[_conv_node(i)])

        store.compact(max_conversation_facts=5)
        second = store.compact(max_conversation_facts=5)

        assert second == 0
        _assert_no_orphans(store)

    def test_non_conversation_facts_embeddings_untouched(
        self, tmp_path: pytest.TempPathFactory, embedder: LocalEmbedder
    ) -> None:
        """compact() must not remove embeddings for non-conv Facts."""
        store = VectorMemoryStore(tmp_path / "mem.kuzu", embedder=embedder)
        for i in range(10):
            store.remember(nodes=[_conv_node(i)])
        # Add 2 non-conv Facts with embeddings.
        for i in range(2):
            store.remember(nodes=[_repo_node(i)])

        store.compact(max_conversation_facts=5)

        remaining_vec_ids = {
            row[0]
            for row in store._vec.execute("SELECT fact_id FROM fact_map").fetchall()
        }
        for i in range(2):
            assert f"repo-{i}" in remaining_vec_ids, (
                f"non-conv repo-{i} embedding was wrongly removed"
            )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fact_ids(store: MemoryStore) -> set[str]:
    """Return the set of all Fact ids currently in the graph."""
    return {str(row[0]) for row in store._rows("MATCH (f:Fact) RETURN f.id")}


def _assert_no_orphans(store: VectorMemoryStore) -> None:
    """Assert no orphaned rows exist between fact_vectors and fact_map."""
    map_count: int = store._vec.execute("SELECT COUNT(*) FROM fact_map").fetchone()[0]
    vec_count: int = store._vec.execute(
        "SELECT COUNT(*) FROM fact_vectors"
    ).fetchone()[0]
    assert map_count == vec_count, (
        f"Orphan detected: fact_map has {map_count} rows, fact_vectors has {vec_count}"
    )
