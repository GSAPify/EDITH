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

from edith.memory.secrets import sanitize_text

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
        "Repo(id STRING PRIMARY KEY, path STRING, remote STRING, "
        "name STRING, org STRING DEFAULT '', summary STRING, language STRING, "
        "last_commit_date STRING)"
    ),
    "Person": (
        "CREATE NODE TABLE IF NOT EXISTS "
        "Person(id STRING PRIMARY KEY, name STRING, gh_handle STRING)"
    ),
    "PR": (
        "CREATE NODE TABLE IF NOT EXISTS "
        "PR(id STRING PRIMARY KEY, title STRING, number INT64, state STRING)"
    ),
    "Fact": (
        "CREATE NODE TABLE IF NOT EXISTS "
        "Fact(id STRING PRIMARY KEY, text STRING, learned_at STRING, source STRING)"
    ),
}

# Edge tables. Kuzu REL tables are typed FROM->TO; a couple of core edges.
_EDGE_SCHEMA: dict[str, str] = {
    "works_on": "CREATE REL TABLE IF NOT EXISTS works_on(FROM Owner TO Project)",
    "owns": (
        "CREATE REL TABLE IF NOT EXISTS owns(FROM Project TO Repo, FROM Repo TO PR)"
    ),
    "knows": "CREATE REL TABLE IF NOT EXISTS knows(FROM Owner TO Person)",
    # authored_by fans out from PR and from Repo to a Person (repo ingestion
    # attributes clones to their owners via gh metadata) -> multi-pair REL table.
    "authored_by": (
        "CREATE REL TABLE IF NOT EXISTS authored_by(FROM PR TO Person, FROM Repo TO Person)"
    ),
    "reviewed_by": "CREATE REL TABLE IF NOT EXISTS reviewed_by(FROM PR TO Person)",
    # relates_to fans out from Fact to several targets -> multi-pair REL table.
    "relates_to": (
        "CREATE REL TABLE IF NOT EXISTS relates_to("
        "FROM Fact TO Project, FROM Fact TO Repo, FROM Fact TO Person, FROM Fact TO PR)"
    ),
}

# Which string properties count as searchable text for the graph-only recall scan.
_TEXT_PROPS: dict[str, tuple[str, ...]] = {
    "Owner": ("name", "email"),
    "Project": ("name", "status"),
    "Repo": ("path", "remote", "name", "summary"),
    "Person": ("name", "gh_handle"),
    "Fact": ("text",),
}

# Display-label prop per node type for the graph snapshot (falls back to id).
_LABEL_PROP: dict[str, str] = {
    "Owner": "name",
    "Project": "name",
    "Repo": "name",
    "Person": "name",
    "PR": "title",
    "Fact": "text",
}

# Kuzu injects these bookkeeping keys into a whole-node RETURN; drop them.
_KUZU_INTERNAL_KEYS = ("_id", "_label")


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


def sanitize_node(node: Node) -> Node:
    """Return ``node`` with every string property run through the secrets filter.

    The never-persist step (north-star §6.1): a credential in any text property
    is redacted before the node is written anywhere.
    """
    cleaned = {
        k: (sanitize_text(v) if isinstance(v, str) else v) for k, v in node.props.items()
    }
    return Node(node.label, node.id, cleaned)


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
        self._migrate_person_gh_handle()

    def _migrate_person_gh_handle(self) -> None:
        """Add ``Person.gh_handle`` to a pre-existing DB (spec 02 §Data model).

        A fresh DB already has the column from ``_NODE_SCHEMA`` so this is a
        no-op; the live 206-node DB predates it, so the guarded ALTER adds it.
        Idempotent either way — ``TABLE_INFO`` returns the column name at index 1
        (row shape: ``[property id, name, type, default, primary key]``).
        """
        columns = {str(row[1]) for row in self._rows("CALL TABLE_INFO('Person') RETURN *")}
        if "gh_handle" not in columns:
            self._run("ALTER TABLE Person ADD gh_handle STRING DEFAULT ''")
        self._migrate_repo_org()

    def _migrate_repo_org(self) -> None:
        """Add ``Repo.org`` to a pre-existing DB and backfill the incumbent org.

        The live graph predates the two-workspace change; its Repo nodes are all
        patterninc. Guarded ALTER (idempotent), then tag any untagged rows so
        finder/filters see the right org. A fresh DB already has the column.
        """
        columns = {str(row[1]) for row in self._rows("CALL TABLE_INFO('Repo') RETURN *")}
        if "org" not in columns:
            self._run("ALTER TABLE Repo ADD org STRING DEFAULT ''")
        self._run("MATCH (r:Repo) WHERE r.org = '' SET r.org = 'patterninc'")

    def node_tables(self) -> set[str]:
        """Return the set of node-table names currently defined."""
        return {
            str(name)
            for name, table_type in self._rows("CALL SHOW_TABLES() RETURN name, type;")
            if table_type == "NODE"
        }

    def rel_tables(self) -> set[str]:
        """Return the set of relationship-table names currently defined."""
        return {
            str(name)
            for name, table_type in self._rows("CALL SHOW_TABLES() RETURN name, type;")
            if table_type == "REL"
        }

    def graph_snapshot(self) -> dict[str, list[dict[str, Any]]]:
        """Export the whole graph as force-graph-shaped JSON.

        Introspective: walks every node table and every REL table currently
        defined, so schema growth (e.g. later repo-ingestion tables) renders
        with no change here. Shape::

            {"nodes": [{"id", "type", "label", "degree", <props>}],
             "links": [{"source", "target", "type"}]}

        ``degree`` is computed in Python from link incidence (undirected count).
        """
        links: list[dict[str, Any]] = []
        for rel in sorted(self.rel_tables()):
            for source, target in self._rows(
                f"MATCH (a)-[:{rel}]->(b) RETURN a.id, b.id"
            ):
                links.append({"source": str(source), "target": str(target), "type": rel})

        degree: dict[str, int] = {}
        for link in links:
            degree[link["source"]] = degree.get(link["source"], 0) + 1
            degree[link["target"]] = degree.get(link["target"], 0) + 1

        nodes: list[dict[str, Any]] = []
        for label in sorted(self.node_tables()):
            for (raw,) in self._rows(f"MATCH (n:{label}) RETURN n"):
                props = {k: v for k, v in raw.items() if k not in _KUZU_INTERNAL_KEYS}
                node_id = str(props["id"])
                label_prop = _LABEL_PROP.get(label)
                display = props.get(label_prop) if label_prop else None
                node: dict[str, Any] = {
                    "id": node_id,
                    "type": label,
                    "label": str(display) if display else node_id,
                    "degree": degree.get(node_id, 0),
                }
                node.update({k: v for k, v in props.items() if k != "id"})
                nodes.append(node)
        return {"nodes": nodes, "links": links}

    def remember(
        self,
        nodes: list[Node] | None = None,
        edges: list[Edge] | None = None,
    ) -> None:
        """Write nodes (upsert by id) then edges. Nodes must exist before edges.

        The never-persist secrets filter runs FIRST (north-star §6.1): every
        string property is sanitized before any write, so a credential is never
        persisted to the graph.
        """
        for node in nodes or []:
            self._upsert_node(sanitize_node(node))
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

    def compact(self, *, max_conversation_facts: int = 500) -> int:
        """Evict the oldest conversation Facts beyond ``max_conversation_facts``.

        Only ``conv-*`` Facts (written by ``Brain._remember_exchange``) are
        considered. Facts from ingest, PR-review, or any other source whose id
        does not start with ``"conv-"`` are never touched.

        Ordering is by ``learned_at`` descending (newest first). Because
        ``learned_at`` is stored as ``str(time.time())`` — a fixed-width
        10-digit integer part followed by a decimal — lexicographic order
        matches numeric recency for all production-written values. Tests must
        seed similarly-formatted values (e.g. ``str(1_720_000_000.0 + i)``).

        Each evictable node is deleted with ``DETACH DELETE`` so any
        ``relates_to`` edges are removed atomically with the node.

        Pure synchronous, no model call, no network. Safe to call from a sync
        shutdown path. Idempotent: a second call with the same limit evicts 0.

        Returns the number of Facts evicted.

        NOTE — summarizing rollup (compressing evicted Facts into a digest) is
        explicitly OUT OF SCOPE here: it requires a model call and therefore
        cannot live on the synchronous shutdown path. That work belongs in a
        separate async compaction pass.
        """
        to_evict = self._conv_facts_to_evict(max_conversation_facts)
        for fact_id in to_evict:
            self._evict_fact_from_graph(fact_id)
        return len(to_evict)

    def _conv_facts_to_evict(self, max_conversation_facts: int) -> list[str]:
        """Ids of the ``conv-*`` Facts beyond the newest ``max_conversation_facts`` — the
        evictable tail, newest-first. Single-sourced so ``MemoryStore.compact`` and
        ``VectorMemoryStore.compact`` share ONE selection query and can't drift."""
        rows = list(
            self._rows(
                "MATCH (f:Fact) WHERE f.id STARTS WITH 'conv-' "
                "RETURN f.id ORDER BY f.learned_at DESC"
            )
        )
        return [str(row[0]) for row in rows[max_conversation_facts:]]

    def _evict_fact_from_graph(self, fact_id: str) -> None:
        """``DETACH DELETE`` a Fact node (removes its ``relates_to`` edges atomically)."""
        self._run("MATCH (f:Fact {id: $id}) DETACH DELETE f", {"id": fact_id})

    def close(self) -> None:
        """Close the connection and DB, releasing the on-disk lock."""
        self._conn.close()
        self._db.close()
