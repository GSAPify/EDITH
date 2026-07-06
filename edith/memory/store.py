"""Memory store — durable graph over embedded Kuzu.

Slice 1 foundation. Implements the graph half of the Memory contract
(north-star §4.3): ``remember(nodes | edges)`` and a Cypher-traversal
``recall(query)``. Vector/semantic recall is layered on in ``vector.py``.

Sync for this run: there is no async consumer yet (edithd/Brain are later
slices) and Kuzu is blocking. When edithd lands, the public Memory contract
becomes ``async``; the wiring here stays the source of truth for the queries.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import kuzu

# The core slice-1 subset of the spec's data model (Data model §Node/Edge types).
# Deliberately not all 10 nodes / 11 edges — enough to prove the pattern.
_NODE_SCHEMA: dict[str, str] = {
    "Owner": (
        "CREATE NODE TABLE IF NOT EXISTS "
        "Owner(id STRING PRIMARY KEY, name STRING, email STRING)"
    ),
    "Project": (
        "CREATE NODE TABLE IF NOT EXISTS "
        "Project(id STRING PRIMARY KEY, name STRING, status STRING)"
    ),
    "Repo": (
        "CREATE NODE TABLE IF NOT EXISTS "
        "Repo(id STRING PRIMARY KEY, path STRING, remote STRING)"
    ),
    "Person": "CREATE NODE TABLE IF NOT EXISTS Person(id STRING PRIMARY KEY, name STRING)",
    "Fact": (
        "CREATE NODE TABLE IF NOT EXISTS "
        "Fact(id STRING PRIMARY KEY, text STRING, learned_at STRING)"
    ),
}

# Edge tables. Kuzu REL tables are typed FROM->TO; a couple of core edges.
_EDGE_SCHEMA: dict[str, str] = {
    "works_on": "CREATE REL TABLE IF NOT EXISTS works_on(FROM Owner TO Project)",
    "owns": "CREATE REL TABLE IF NOT EXISTS owns(FROM Project TO Repo)",
    "knows": "CREATE REL TABLE IF NOT EXISTS knows(FROM Owner TO Person)",
    # relates_to fans out from Fact to several targets -> multi-pair REL table.
    "relates_to": (
        "CREATE REL TABLE IF NOT EXISTS relates_to("
        "FROM Fact TO Project, FROM Fact TO Repo, FROM Fact TO Person)"
    ),
}

# Which string properties count as searchable text for the graph-only recall scan.
_TEXT_PROPS: dict[str, tuple[str, ...]] = {
    "Owner": ("name", "email"),
    "Project": ("name", "status"),
    "Repo": ("path", "remote"),
    "Person": ("name",),
    "Fact": ("text",),
}


@dataclass(frozen=True)
class Node:
    """A graph node to remember. ``props`` maps to the table's columns."""

    label: str
    id: str
    props: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class Edge:
    """A typed relationship between two existing nodes."""

    label: str
    from_label: str
    from_id: str
    to_label: str
    to_id: str


class MemoryStore:
    """Embedded-Kuzu graph store. One instance owns one DB directory."""

    def __init__(self, db_path: str | Path) -> None:
        self._path = str(db_path)
        self._db = kuzu.Database(self._path)
        self._conn = kuzu.Connection(self._db)
        self._create_schema()

    def _run(self, query: str, parameters: dict[str, object] | None = None) -> kuzu.QueryResult:
        """Execute a single statement and return its result.

        ``Connection.execute`` is typed as possibly returning a list (for
        multi-statement scripts); we only ever run one statement, so narrow it.
        """
        result = self._conn.execute(query, parameters=parameters or {})
        assert isinstance(result, kuzu.QueryResult)  # noqa: S101 - single-statement invariant
        return result

    def _rows(
        self, query: str, parameters: dict[str, object] | None = None
    ) -> Iterator[list[Any]]:
        # All queries here use positional RETURN, so each row is a list.
        result = self._run(query, parameters)
        while result.has_next():
            yield result.get_next()  # type: ignore[misc]

    def _create_schema(self) -> None:
        for ddl in _NODE_SCHEMA.values():
            self._run(ddl)
        for ddl in _EDGE_SCHEMA.values():
            self._run(ddl)

    def node_tables(self) -> set[str]:
        """Return the set of node-table names currently defined."""
        return {
            str(name)
            for name, table_type in self._rows("CALL SHOW_TABLES() RETURN name, type;")
            if table_type == "NODE"
        }

    def remember(
        self,
        nodes: list[Node] | None = None,
        edges: list[Edge] | None = None,
    ) -> None:
        """Write nodes (upsert by id) then edges. Nodes must exist before edges."""
        for node in nodes or []:
            self._upsert_node(node)
        for edge in edges or []:
            self._create_edge(edge)

    def _upsert_node(self, node: Node) -> None:
        props = {"id": node.id, **node.props}
        # MERGE on the primary key makes remember idempotent per node id;
        # explicit per-property SET (Kuzu Cypher has no `SET n += {map}`).
        sets = ", ".join(f"n.{k} = ${k}" for k in node.props)
        stmt = f"MERGE (n:{node.label} {{id: $id}})"
        if sets:
            stmt += f" SET {sets}"
        self._run(stmt, props)

    def _create_edge(self, edge: Edge) -> None:
        self._run(
            f"MATCH (a:{edge.from_label} {{id: $from_id}}), "
            f"(b:{edge.to_label} {{id: $to_id}}) "
            f"MERGE (a)-[:{edge.label}]->(b)",
            {"from_id": edge.from_id, "to_id": edge.to_id},
        )

    def recall(self, query: str) -> list[dict[str, object]]:
        """Graph-only recall: case-insensitive substring scan over text props.

        This is the structural signal (spec §Retrieval strategy #1/#3 minus
        vector). It is deterministic, model-free, and always current — the
        design-around for the static HNSW index. Vector fusion is layered on
        in ``vector.py``.
        """
        needle = query.lower()
        hits: list[dict[str, object]] = []
        seen: set[tuple[str, str]] = set()
        anchors: list[tuple[str, str]] = []  # (label, id) of direct text matches

        for label, props in _TEXT_PROPS.items():
            query_cypher = f"MATCH (n:{label}) RETURN n.id, " + ", ".join(
                f"n.{p}" for p in props
            )
            for row in self._rows(query_cypher):
                node_id = str(row[0])
                values = tuple(row[1:])
                text_blob = " ".join(str(v) for v in values if v is not None)
                if needle in text_blob.lower():
                    anchors.append((label, node_id))
                    self._add_hit(hits, seen, label, node_id, props, values)

        # Signal #1: 1-hop traversal from each anchor to pull the structurally
        # relevant neighborhood (e.g. Facts that relate_to a matched Project).
        for anchor_label, anchor_id in anchors:
            self._pull_related_facts(hits, seen, anchor_label, anchor_id)
        return hits

    @staticmethod
    def _add_hit(
        hits: list[dict[str, object]],
        seen: set[tuple[str, str]],
        label: str,
        node_id: str,
        props: tuple[str, ...],
        values: tuple[object, ...],
    ) -> None:
        key = (label, node_id)
        if key in seen:
            return
        seen.add(key)
        record: dict[str, object] = {"label": label, "id": node_id}
        for prop, value in zip(props, values, strict=True):
            record[prop] = value
        hits.append(record)

    def _pull_related_facts(
        self,
        hits: list[dict[str, object]],
        seen: set[tuple[str, str]],
        anchor_label: str,
        anchor_id: str,
    ) -> None:
        # Facts point --relates_to--> {Project, Repo, Person}; traverse inbound.
        rows = self._rows(
            f"MATCH (f:Fact)-[:relates_to]->(n:{anchor_label} {{id: $id}}) "
            "RETURN f.id, f.text",
            {"id": anchor_id},
        )
        for fid, text in rows:
            self._add_hit(hits, seen, "Fact", str(fid), ("text",), (text,))

    def count(self, label: str) -> int:
        """Number of nodes of a given label."""
        for row in self._rows(f"MATCH (n:{label}) RETURN count(n)"):
            return int(row[0])  # type: ignore[arg-type]
        return 0

    def close(self) -> None:
        """Close the connection and DB, releasing the on-disk lock."""
        self._conn.close()
        self._db.close()
