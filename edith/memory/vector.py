"""Semantic recall over a sqlite-vec vector store.

``VectorMemoryStore`` extends the graph ``MemoryStore`` with an *embedded*
sqlite-vec index for the embeddable node types (Fact for this slice). The
vectors live in a sqlite file alongside the Kuzu DB — Kuzu owns the graph,
sqlite-vec owns the vectors (spec §Storage decision, revised Session 2):

- ``remember`` writes each Fact to the graph AND its embedding to sqlite-vec,
  in the same write path — a Fact's graph node and its vector row are written
  together or the whole ``remember`` raises.
- ``semantic_recall`` runs a sqlite-vec KNN query for top-k similar Facts.
- ``build_vector_index`` is retained as a no-op: sqlite-vec inserts are
  incremental, so there is no build-once step. A Fact remembered after the
  store already exists is immediately searchable — the capability Kuzu's
  build-once HNSW index lacked.

**id-mapping.** sqlite-vec rows are keyed by integer ``rowid``; Kuzu Fact nodes
are keyed by string ``id``. A companion ``fact_map(rowid ↔ fact_id)`` table,
written in the same ``remember`` as the vector row, ties them together.

**Atomicity is honest, not cross-engine.** There is no two-phase commit across
two embedded engines. The graph write (Kuzu) happens first; the sqlite side is
wrapped in a single transaction. If the sqlite write fails the whole
``remember`` raises (so the caller sees the failure), rather than silently
leaving the vector store behind the graph.
"""

from __future__ import annotations

import sqlite3
import struct
from pathlib import Path

import sqlite_vec

from edith.memory.embeddings import Embedder, LocalEmbedder
from edith.memory.secrets import sanitize_text
from edith.memory.store import Edge, MemoryStore, Node, sanitize_node


def _pack(vector: list[float]) -> bytes:
    """Pack a float vector into sqlite-vec's little-endian float32 blob format."""
    return struct.pack(f"{len(vector)}f", *vector)


class VectorMemoryStore(MemoryStore):
    """Graph store (Kuzu) + embedded sqlite-vec index for semantic recall."""

    def __init__(
        self,
        db_path: str | Path,
        embedder: Embedder | None = None,
    ) -> None:
        super().__init__(db_path)
        self._embedder: Embedder = embedder or LocalEmbedder()
        self._vec_path = self._sibling_vec_path(Path(db_path))
        self._vec = self._open_vec_store()
        self._create_vec_schema()

    @staticmethod
    def _sibling_vec_path(db_path: Path) -> str:
        # Kuzu stores its DB as a file (or a directory on some versions); put the
        # sqlite-vec file deterministically alongside it so a reopen re-finds it.
        return str(db_path.parent / f"{db_path.name}.vec.sqlite")

    def _open_vec_store(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._vec_path)
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        return conn

    def _create_vec_schema(self) -> None:
        dim = self._embedder.dim
        self._vec.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS fact_vectors USING vec0(embedding FLOAT[{dim}])"
        )
        # Companion id-map: sqlite rowid <-> Kuzu Fact string id. Unique on
        # fact_id so re-remembering the same Fact upserts one vector row.
        self._vec.execute(
            "CREATE TABLE IF NOT EXISTS fact_map("
            "rowid INTEGER PRIMARY KEY, fact_id TEXT UNIQUE NOT NULL, text TEXT NOT NULL)"
        )
        self._vec.commit()

    def remember(
        self,
        nodes: list[Node] | None = None,
        edges: list[Edge] | None = None,
    ) -> None:
        """Write nodes/edges to the graph, and each Fact's embedding to sqlite-vec.

        Secrets are stripped FIRST (before embedding), so a credential never
        reaches the vector store either. The graph write and the vector write
        share one ``remember`` boundary: the sqlite writes run in a single
        transaction and any failure raises rather than desyncing the stores.
        """
        clean_nodes = [sanitize_node(n) for n in (nodes or [])]
        super().remember(nodes=clean_nodes, edges=edges)

        facts = [n for n in clean_nodes if n.label == "Fact"]
        if not facts:
            return
        try:
            for fact in facts:
                text = str(fact.props.get("text", ""))
                self._upsert_vector(fact.id, text)
            self._vec.commit()
        except sqlite3.Error:
            self._vec.rollback()
            raise

    def _upsert_vector(self, fact_id: str, text: str) -> None:
        # Re-remembering a Fact replaces its vector row; keep the id-map unique.
        row = self._vec.execute(
            "SELECT rowid FROM fact_map WHERE fact_id = ?", (fact_id,)
        ).fetchone()
        vec = _pack(self._embedder.embed(text))
        if row is not None:
            rowid = int(row[0])
            self._vec.execute("DELETE FROM fact_vectors WHERE rowid = ?", (rowid,))
            self._vec.execute(
                "INSERT INTO fact_vectors(rowid, embedding) VALUES (?, ?)", (rowid, vec)
            )
            self._vec.execute(
                "UPDATE fact_map SET text = ? WHERE rowid = ?", (text, rowid)
            )
            return
        cursor = self._vec.execute(
            "INSERT INTO fact_map(fact_id, text) VALUES (?, ?)", (fact_id, text)
        )
        rowid = int(cursor.lastrowid or 0)
        self._vec.execute(
            "INSERT INTO fact_vectors(rowid, embedding) VALUES (?, ?)", (rowid, vec)
        )

    def compact(self, *, max_conversation_facts: int = 500) -> int:
        """Evict old conv-* Facts from the graph AND their embeddings from sqlite-vec.

        Delegates selection and graph deletion to ``MemoryStore.compact``, then
        removes the corresponding rows from ``fact_map`` and ``fact_vectors`` so
        no orphaned embeddings remain. The sqlite side runs in a single
        transaction; any failure rolls back and re-raises.

        No embed() call — deletion needs no embedding. Pure/synchronous.
        """
        # Collect the ids that will be evicted BEFORE deleting from the graph,
        # so we can look them up in fact_map by fact_id.
        rows = list(
            self._rows(
                "MATCH (f:Fact) WHERE f.id STARTS WITH 'conv-' "
                "RETURN f.id ORDER BY f.learned_at DESC"
            )
        )
        evict_ids = [str(row[0]) for row in rows[max_conversation_facts:]]
        if not evict_ids:
            return 0

        # Delete from the graph (DETACH DELETE handles edges).
        for fact_id in evict_ids:
            self._run(
                "MATCH (f:Fact {id: $id}) DETACH DELETE f",
                {"id": fact_id},
            )

        # Delete from sqlite-vec (fact_map + fact_vectors) in one transaction.
        try:
            for fact_id in evict_ids:
                row = self._vec.execute(
                    "SELECT rowid FROM fact_map WHERE fact_id = ?", (fact_id,)
                ).fetchone()
                if row is not None:
                    rowid = int(row[0])
                    self._vec.execute(
                        "DELETE FROM fact_vectors WHERE rowid = ?", (rowid,)
                    )
                    self._vec.execute(
                        "DELETE FROM fact_map WHERE rowid = ?", (rowid,)
                    )
            self._vec.commit()
        except Exception:
            self._vec.rollback()
            raise

        return len(evict_ids)

    def build_vector_index(self) -> None:
        """No-op: sqlite-vec inserts are incremental (no build-once step).

        Retained so callers written against the previous build-once Kuzu impl
        keep working; the moment a Fact is remembered it is searchable.
        """

    def backfill_embeddings(self) -> int:
        """Embed existing graph ``Fact`` nodes that have no vector row yet.

        Backfills a store that was written graph-only (e.g. by a plain
        ``MemoryStore``): reads every ``Fact`` from the Kuzu graph and inserts
        its embedding into sqlite-vec using the LOCAL embedder — NO model /
        Bifrost calls. Idempotent: Facts already in ``fact_map`` are skipped, so
        re-running embeds nothing new. Returns the number of Facts embedded.

        Sanitize runs FIRST on each text (defence-in-depth: the never-persist
        guarantee holds on the backfill path too), so no credential is embedded.
        """
        embedded = 0
        try:
            for fact_id, text in self._rows("MATCH (f:Fact) RETURN f.id, f.text"):
                fid = str(fact_id)
                row = self._vec.execute(
                    "SELECT 1 FROM fact_map WHERE fact_id = ?", (fid,)
                ).fetchone()
                if row is not None:
                    continue  # already embedded — idempotent skip
                self._upsert_vector(fid, sanitize_text(str(text or "")))
                embedded += 1
            self._vec.commit()
        except sqlite3.Error:
            self._vec.rollback()
            raise
        return embedded

    def semantic_recall(self, query: str, k: int = 5) -> list[dict[str, object]]:
        """Top-k Facts by cosine-ish (L2) distance to ``query``, via sqlite-vec KNN."""
        query_vec = _pack(self._embedder.embed(query))
        rows = self._vec.execute(
            "SELECT v.rowid, m.fact_id, m.text, v.distance "
            "FROM fact_vectors v JOIN fact_map m ON m.rowid = v.rowid "
            "WHERE v.embedding MATCH ? AND k = ? ORDER BY v.distance",
            (query_vec, k),
        ).fetchall()
        return [
            {"label": "Fact", "id": str(fact_id), "text": text, "distance": float(dist)}
            for _rowid, fact_id, text, dist in rows
        ]

    def close(self) -> None:
        """Close the sqlite-vec connection, then the Kuzu graph store."""
        self._vec.close()
        super().close()
