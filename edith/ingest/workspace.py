"""Workspace metadata ingest — graph every repo in a GitHub org, no model calls.

The scalable, ~free path to "every repo in the graph" (spec 08 extension). Instead of
cloning + a per-repo model classification (thousands of calls for a 1600-repo org), this
enumerates the org via the GitHub API and writes a structural ``Repo`` node + one embedded
``gh_description`` Fact per repo. Deep model extraction stays ON DEMAND — ``resolve_repo``
deep-extracts a repo the moment the owner actually asks about it.

Two orgs live in ONE graph: every node is ``org``-tagged, and ids are org-scoped (except the
incumbent ``patterninc``, unprefixed for back-compat). The finder ranks across both or filters
by org.

Writes are SERIAL through one ``VectorMemoryStore`` — Kuzu embedded is single-writer, so this
must never be parallelised across processes/agents. The repo *lister* is injected so the whole
module is unit-tested with no network.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from edith.ingest.discover import DiscoveredRepo
from edith.ingest.graph_map import build_metadata_graph
from edith.memory.embeddings import Embedder
from edith.memory.vector import VectorMemoryStore

# org -> list of flat metadata dicts (name/description/topics/language/html_url/pushed_at/archived)
RepoLister = Callable[[str], list[dict[str, object]]]

_DEFAULT_DATA_DIR = "~/.edith/data"


@dataclass
class WorkspaceReport:
    org: str
    repos_written: int = 0
    facts_written: int = 0
    skipped_archived: int = 0

    def render(self) -> str:
        return (
            f"workspace ingest — org={self.org}\n"
            f"  repos written:    {self.repos_written}\n"
            f"  facts written:    {self.facts_written}\n"
            f"  archived skipped: {self.skipped_archived}"
        )


def _discovered(org: str, meta: dict[str, object]) -> DiscoveredRepo:
    return DiscoveredRepo(
        name=str(meta.get("name", "")),
        path="",  # metadata-only: not cloned
        remote=str(meta.get("html_url", "")),
        last_commit_date=str(meta.get("pushed_at", "") or ""),
        org=org,
    )


def ingest_workspace(
    org: str,
    *,
    data_dir: str | Path = _DEFAULT_DATA_DIR,
    lister: RepoLister | None = None,
    embedder: Embedder | None = None,
    include_archived: bool = False,
    names: list[str] | None = None,
    limit: int | None = None,
) -> WorkspaceReport:
    """Enumerate ``org`` and write a metadata Repo node + description Fact for each."""
    repos = (lister or _gh_list_repos)(org)
    if names is not None:
        wanted = set(names)
        repos = [r for r in repos if str(r.get("name", "")) in wanted]

    report = WorkspaceReport(org=org)
    db_path = Path(data_dir).expanduser() / "memory.kuzu"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = VectorMemoryStore(db_path, embedder=embedder)
    try:
        written = 0
        for meta in repos:
            if not include_archived and bool(meta.get("archived")):
                report.skipped_archived += 1
                continue
            if limit is not None and written >= limit:
                break
            repo = _discovered(org, meta)
            if not repo.name:
                continue
            raw_topics = meta.get("topics")
            topics = [str(t) for t in raw_topics if t] if isinstance(raw_topics, list) else []
            nodes, edges = build_metadata_graph(
                repo,
                description=str(meta.get("description", "") or ""),
                topics=topics,
                language=str(meta.get("language", "") or ""),
            )
            store.remember(nodes=nodes, edges=edges)  # SERIAL — single Kuzu writer
            written += 1
            report.facts_written += sum(1 for n in nodes if n.label == "Fact")
        report.repos_written = written
    finally:
        store.close()
    return report


def _gh_list_repos(org: str) -> list[dict[str, object]]:
    """Enumerate every repo in ``org`` via the GitHub API (~17 paginated calls for 1600)."""
    proc = subprocess.run(
        [
            "gh", "api", f"orgs/{org}/repos?per_page=100&type=all", "--paginate",
            "--jq",
            ".[] | {name, description, topics, language, html_url, pushed_at, archived}",
        ],
        capture_output=True, text=True, check=True,
    )
    return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
