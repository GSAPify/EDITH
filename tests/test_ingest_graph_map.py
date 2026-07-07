"""graph_map: extracted knowledge -> Node/Edge, remembered to a temp Kuzu store.

Mapping is deterministic and model-free; extraction is OPTIONAL so ``--dry-run``
(no model calls) still produces structural nodes + CLAUDE.md/README Facts. Owner
standards: Facts carry a ``source`` prop; edges use existing tables
(owns / authored_by / relates_to).
"""

from __future__ import annotations

from pathlib import Path

from edith.ingest.discover import DiscoveredRepo
from edith.ingest.extract import Extraction
from edith.ingest.fetch import RepoDocs
from edith.ingest.graph_map import build_graph, map_and_remember
from edith.memory.store import MemoryStore


def _repo() -> DiscoveredRepo:
    return DiscoveredRepo(
        name="portal",
        path="/x/portal",
        remote="https://github.com/patterninc/portal.git",
        last_commit_date="2026-07-01",
    )


def _docs() -> RepoDocs:
    return RepoDocs(
        name="portal",
        path="/x/portal",
        readme="Onboarding portal for brands.",
        claude_md="Use the shared ingest pipeline.",
        metadata={"primaryLanguage": "Python"},
    )


def test_build_graph_without_extraction_still_makes_repo_and_source_facts() -> None:
    nodes, edges = build_graph(_repo(), _docs(), extraction=None)

    labels = {n.label for n in nodes}
    assert "Repo" in labels
    fact_sources = {n.props.get("source") for n in nodes if n.label == "Fact"}
    assert "readme" in fact_sources
    assert "claude_md" in fact_sources
    # Repo carries last_commit_date for the incremental skip check.
    repo_node = next(n for n in nodes if n.label == "Repo")
    assert repo_node.props["last_commit_date"] == "2026-07-01"


def test_build_graph_with_extraction_adds_people_and_extraction_facts() -> None:
    extraction = Extraction(
        summary="onboarding portal",
        relevance=0.9,
        purpose="onboard brands",
        components=["api", "worker"],
        stack=["python"],
        owners=["Akhil"],
        deep=True,
    )

    nodes, edges = build_graph(_repo(), _docs(), extraction=extraction)

    person_names = {n.props.get("name") for n in nodes if n.label == "Person"}
    assert "Akhil" in person_names
    edge_labels = {e.label for e in edges}
    assert "authored_by" in edge_labels  # Repo -> Person
    assert "relates_to" in edge_labels  # Fact -> Repo
    extraction_facts = [
        n for n in nodes if n.label == "Fact" and n.props.get("source") == "extraction"
    ]
    assert extraction_facts


def test_map_and_remember_writes_to_store(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.kuzu")
    try:
        extraction = Extraction(
            summary="s", relevance=0.9, purpose="p", owners=["Akhil"], deep=True
        )
        written = map_and_remember(store, _repo(), _docs(), extraction=extraction)

        assert written > 0
        assert store.count("Repo") == 1
        assert store.count("Person") == 1
        assert store.count("Fact") >= 2
        hits = store.recall("portal")
        assert any(h["label"] == "Repo" for h in hits)
    finally:
        store.close()
