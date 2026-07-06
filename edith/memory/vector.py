"""Semantic recall over Kuzu's native HNSW vector index.

``VectorMemoryStore`` extends the graph ``MemoryStore`` with an embedding
column on the embeddable node types (Fact for this slice), the local embedder,
and the Kuzu VECTOR extension:

- ``remember`` embeds each Fact's text and stores it as ``FLOAT[384]``.
- ``rebuild_vector_index`` (re)builds the static HNSW index over those rows.
- ``semantic_recall`` runs ``QUERY_VECTOR_INDEX`` for top-k similar Facts.

The index is *static* (Kuzu exposes build + query, no incremental update), so
it is built after rows exist and rebuilt on demand. Between rebuilds, the
graph-only ``recall`` (inherited) keeps freshly-written facts findable — the
spec's design-around, not a bolt-on.
"""

from __future__ import annotations

from pathlib import Path

from edith.memory.embeddings import Embedder, LocalEmbedder
from edith.memory.store import Edge, MemoryStore, Node, sanitize_node

_FACT_INDEX = "fact_embedding_idx"


class VectorMemoryStore(MemoryStore):
    """Graph store + native Kuzu HNSW vector index for semantic recall."""

    def __init__(
        self,
        db_path: str | Path,
        embedder: Embedder | None = None,
    ) -> None:
        super().__init__(db_path)
        self._embedder: Embedder = embedder or LocalEmbedder()
        self._index_built = False
        self._run("INSTALL vector")
        self._run("LOAD vector")
        self._ensure_embedding_column()

    def _ensure_embedding_column(self) -> None:
        # Add a fixed-width float embedding column to Fact if not already there.
        dim = self._embedder.dim
        existing = {row[1] for row in self._rows("CALL TABLE_INFO('Fact') RETURN *;")}
        if "embedding" not in existing:
            self._run(f"ALTER TABLE Fact ADD embedding FLOAT[{dim}]")

    def remember(
        self,
        nodes: list[Node] | None = None,
        edges: list[Edge] | None = None,
    ) -> None:
        """Write nodes/edges, embedding each Fact's *sanitized* text into its row.

        Secrets are stripped FIRST (before embedding) so a credential never
        reaches the vector store either.
        """
        prepared: list[Node] = []
        for node in nodes or []:
            clean = sanitize_node(node)
            if clean.label == "Fact" and "embedding" not in clean.props:
                text = str(clean.props.get("text", ""))
                vec = self._embedder.embed(text)
                prepared.append(Node(clean.label, clean.id, {**clean.props, "embedding": vec}))
            else:
                prepared.append(clean)
        super().remember(nodes=prepared, edges=edges)

    def rebuild_vector_index(self) -> None:
        """(Re)build the static HNSW index over Fact.embedding."""
        if self._index_built:
            self._run(f"CALL DROP_VECTOR_INDEX('Fact', '{_FACT_INDEX}')")
        self._run(
            f"CALL CREATE_VECTOR_INDEX('Fact', '{_FACT_INDEX}', 'embedding', metric := 'cosine')"
        )
        self._index_built = True

    def semantic_recall(self, query: str, k: int = 5) -> list[dict[str, object]]:
        """Top-k Facts by cosine similarity to ``query``.

        Returns ``[]`` if no index has been built yet — callers rely on the
        inherited graph ``recall`` to cover that window (static-index caveat).
        """
        if not self._index_built:
            return []
        query_vec = self._embedder.embed(query)
        rows = self._rows(
            f"CALL QUERY_VECTOR_INDEX('Fact', '{_FACT_INDEX}', $q, $k) "
            "RETURN node.id, node.text, distance ORDER BY distance",
            {"q": query_vec, "k": k},
        )
        return [
            {"label": "Fact", "id": str(fid), "text": text, "distance": float(dist)}
            for fid, text, dist in rows
        ]
