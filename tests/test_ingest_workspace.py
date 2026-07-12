"""Workspace metadata ingest + org-scoped graph mapping (spec 08 extension).

Covers the two-workspace-in-one-graph correctness: org-tagged nodes, org-scoped ids
(so a name shared across orgs can't collide), additive upsert that never blanks a rich
deep-ingested node, and the metadata description Fact.
"""

from __future__ import annotations

import hashlib

from edith.ingest.discover import DiscoveredRepo
from edith.ingest.graph_map import build_graph, build_metadata_graph
from edith.ingest.workspace import ingest_workspace
from edith.memory.store import Node
from edith.memory.vector import VectorMemoryStore


class _FakeEmbedder:
    """Deterministic tiny embedder (duck-typed) — no fastembed load, keeps tests fast."""

    dim = 8

    def embed(self, text: str) -> list[float]:
        digest = hashlib.sha1(text.encode()).digest()  # noqa: S324
        return [b / 255.0 for b in digest[: self.dim]]


def _repo(name: str, org: str = "patterninc") -> DiscoveredRepo:
    return DiscoveredRepo(name=name, path="", remote=f"https://github.com/{org}/{name}",
                          last_commit_date="2026-01-01", org=org)


# --- pure mapping -----------------------------------------------------------

def test_patterninc_id_is_unprefixed() -> None:
    nodes, _ = build_graph(_repo("iserve", "patterninc"), _docs(), extraction=None)
    assert nodes[0].id == "repo-iserve"
    assert nodes[0].props["org"] == "patterninc"


def test_other_org_id_is_scoped() -> None:
    nodes, _ = build_graph(_repo("amplifi-api", "ampmedia"), _docs(), extraction=None)
    assert nodes[0].id == "repo-ampmedia-amplifi-api"
    assert nodes[0].props["org"] == "ampmedia"


def test_metadata_build_omits_summary_and_makes_gh_description_fact() -> None:
    nodes, edges = build_metadata_graph(
        _repo("triguardian"), description="A guardian service", topics=["security", "go"],
        language="Go",
    )
    repo_node = next(n for n in nodes if n.label == "Repo")
    # summary is RESERVED for deep extraction — must NOT be written by the metadata pass.
    assert "summary" not in repo_node.props
    assert repo_node.props["language"] == "Go"
    facts = [n for n in nodes if n.label == "Fact"]
    assert len(facts) == 1
    assert facts[0].props["source"] == "gh_description"
    assert "guardian service" in facts[0].props["text"]
    assert "security" in facts[0].props["text"]  # topics folded in
    assert edges[0].label == "relates_to"


def _docs():
    from edith.ingest.fetch import RepoDocs
    return RepoDocs(name="x", path="", readme="", claude_md="", metadata={})


# --- store-level: additive upsert never clobbers a rich node ----------------

def test_metadata_pass_does_not_blank_a_rich_summary(tmp_path) -> None:
    store = VectorMemoryStore(tmp_path / "mem.kuzu", embedder=_FakeEmbedder())
    try:
        # A deep-ingested repo with a rich summary.
        store.remember(nodes=[Node("Repo", "repo-iserve",
                                    {"name": "iserve", "org": "patterninc",
                                     "summary": "Deep Opus summary of iserve"})])
        # Metadata pass over the SAME repo (no summary in props).
        nodes, _ = build_metadata_graph(_repo("iserve"), "thin gh description", [], "Python")
        store.remember(nodes=nodes)

        snap = store.graph_snapshot()
        iserve = next(n for n in snap["nodes"] if n["id"] == "repo-iserve")
        assert iserve["summary"] == "Deep Opus summary of iserve"  # preserved
        assert iserve["language"] == "Python"  # additively enriched
    finally:
        store.close()


# --- workspace ingest -------------------------------------------------------

def _lister(repos):
    return lambda org: repos


def test_ingests_with_org_tag_and_skips_archived(tmp_path) -> None:
    repos = [
        {"name": "iserve", "description": "d1", "topics": [], "language": "Go",
         "html_url": "u", "pushed_at": "2026-01-01", "archived": False},
        {"name": "old-thing", "description": "d2", "topics": [], "language": "",
         "html_url": "u", "pushed_at": "", "archived": True},
    ]
    report = ingest_workspace("patterninc", data_dir=tmp_path, lister=_lister(repos),
                              embedder=_FakeEmbedder())
    assert report.repos_written == 1
    assert report.skipped_archived == 1


def test_two_orgs_same_name_do_not_collide(tmp_path) -> None:
    pi = [{"name": "POC-CI-Check", "description": "pattern one", "topics": [],
           "language": "", "html_url": "u", "pushed_at": "", "archived": False}]
    amp = [{"name": "POC-CI-Check", "description": "amp one", "topics": [],
            "language": "", "html_url": "u", "pushed_at": "", "archived": False}]
    ingest_workspace("patterninc", data_dir=tmp_path, lister=_lister(pi), embedder=_FakeEmbedder())
    ingest_workspace("ampmedia", data_dir=tmp_path, lister=_lister(amp), embedder=_FakeEmbedder())

    store = VectorMemoryStore(tmp_path / "memory.kuzu", embedder=_FakeEmbedder())
    try:
        ids = {n["id"] for n in store.graph_snapshot()["nodes"] if n["type"] == "Repo"}
    finally:
        store.close()
    assert "repo-POC-CI-Check" in ids            # patterninc, unprefixed
    assert "repo-ampmedia-POC-CI-Check" in ids    # ampmedia, scoped — distinct node
