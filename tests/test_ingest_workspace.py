"""Workspace metadata ingest + org-scoped graph mapping (spec 08 extension).

Covers the two-workspace-in-one-graph correctness: org-tagged nodes, org-scoped ids
(so a name shared across orgs can't collide), additive upsert that never blanks a rich
deep-ingested node, and the metadata description Fact.
"""

from __future__ import annotations

import hashlib
import subprocess

import pytest

from edith.ingest.discover import DiscoveredRepo
from edith.ingest.graph_map import build_graph, build_metadata_graph
from edith.ingest.workspace import _gh_list_repos, ingest_workspace
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
    text = facts[0].props["text"]
    assert isinstance(text, str)
    assert "guardian service" in text
    assert "security" in text  # topics folded in
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


# --- injected-store seam (spec 08 item 4: shared handle for the daemon's scheduled refresh) --

class _TrackingStore(VectorMemoryStore):
    """Wraps VectorMemoryStore to record whether/how many times close() ran."""

    def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        super().__init__(*args, **kwargs)
        self.closed = False

    def close(self) -> None:
        self.closed = True
        super().close()


def test_injected_store_is_reused_and_not_closed(tmp_path) -> None:
    store = _TrackingStore(tmp_path / "mem.kuzu", embedder=_FakeEmbedder())
    repos = [{"name": "iserve", "description": "d1", "topics": [], "language": "Go",
              "html_url": "u", "pushed_at": "2026-01-01", "archived": False}]
    try:
        report = ingest_workspace("patterninc", lister=_lister(repos), store=store)
        assert report.repos_written == 1
        assert store.closed is False  # caller owns it — ingest_workspace must not close it

        # the SAME handle wrote the data — still usable after the call returns.
        ids = {n["id"] for n in store.graph_snapshot()["nodes"] if n["type"] == "Repo"}
        assert "repo-iserve" in ids
    finally:
        store.close()


def test_default_path_still_builds_and_closes_its_own_store(tmp_path, monkeypatch) -> None:
    """No injected store -> existing behaviour is unchanged: build one here, close it here."""
    created: list[_TrackingStore] = []

    def factory(db_path, embedder=None):  # noqa: ANN001
        inst = _TrackingStore(db_path, embedder=embedder)
        created.append(inst)
        return inst

    monkeypatch.setattr("edith.ingest.workspace.VectorMemoryStore", factory)
    repos = [{"name": "iserve", "description": "d1", "topics": [], "language": "Go",
              "html_url": "u", "pushed_at": "2026-01-01", "archived": False}]

    report = ingest_workspace(
        "patterninc", data_dir=tmp_path, lister=_lister(repos), embedder=_FakeEmbedder()
    )

    assert report.repos_written == 1
    assert len(created) == 1
    assert created[0].closed is True


# --- _gh_list_repos retry (spec 08 item 4: flakiness, not rate limits) ----------------------

def test_gh_list_repos_retries_transient_failure_then_succeeds(monkeypatch) -> None:
    calls = {"n": 0}
    ok = subprocess.CompletedProcess(
        args=["gh"], returncode=0,
        stdout='{"name": "iserve", "description": "d", "topics": [], "language": "Go", '
               '"html_url": "u", "pushed_at": "2026-01-01", "archived": false}\n',
        stderr="",
    )

    def flaky_run(*args, **kwargs):  # noqa: ANN002, ANN003
        calls["n"] += 1
        if calls["n"] < 3:
            raise subprocess.CalledProcessError(1, "gh")
        return ok

    monkeypatch.setattr("edith.ingest.workspace.subprocess.run", flaky_run)

    repos = _gh_list_repos("patterninc")

    assert calls["n"] == 3           # two transient failures, then success
    assert repos[0]["name"] == "iserve"


def test_gh_list_repos_reraises_after_exhausting_retries(monkeypatch) -> None:
    calls = {"n": 0}

    def always_fails(*args, **kwargs):  # noqa: ANN002, ANN003
        calls["n"] += 1
        raise subprocess.CalledProcessError(1, "gh")

    monkeypatch.setattr("edith.ingest.workspace.subprocess.run", always_fails)

    with pytest.raises(subprocess.CalledProcessError):
        _gh_list_repos("patterninc")

    assert calls["n"] == 3           # stop_after_attempt(3), then reraise
