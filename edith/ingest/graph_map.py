"""Map extracted repo knowledge to Memory nodes/edges (spec 08 §Schema mapping).

Deterministic and model-free. Extraction is OPTIONAL: with it, we add Person
nodes for owners and rich extraction Facts; without it (``--dry-run`` /
low-relevance repos) we still emit the structural Repo node and the README /
CLAUDE.md provenance Facts. Facts carry a ``source`` prop
(``readme`` / ``claude_md`` / ``extraction``) per the spec.

Edges reuse existing tables: ``authored_by`` (Repo -> Person owner),
``relates_to`` (Fact -> Repo). ``remember`` re-runs the never-persist filter, so
nothing here can persist a secret even if an upstream redact step were skipped.
"""

from __future__ import annotations

import hashlib

from edith.ingest.discover import DiscoveredRepo
from edith.ingest.extract import Extraction
from edith.ingest.fetch import RepoDocs
from edith.memory.store import Edge, MemoryStore, Node

_MAX_FACT_CHARS = 600


def _repo_id(repo: DiscoveredRepo) -> str:
    return f"repo-{repo.name}"


def _fact_id(repo: DiscoveredRepo, source: str, seq: int) -> str:
    digest = hashlib.sha1(f"{repo.name}:{source}:{seq}".encode()).hexdigest()[:12]  # noqa: S324
    return f"fact-{digest}"


def _person_id(name: str) -> str:
    slug = name.strip().lower().replace(" ", "-")
    return f"person-{slug}"


def _clip(text: str) -> str:
    text = text.strip()
    return text if len(text) <= _MAX_FACT_CHARS else text[:_MAX_FACT_CHARS].rstrip() + "…"


def build_graph(
    repo: DiscoveredRepo,
    docs: RepoDocs,
    extraction: Extraction | None,
) -> tuple[list[Node], list[Edge]]:
    """Build the (nodes, edges) for one repo. ``extraction`` may be ``None``."""
    nodes: list[Node] = []
    edges: list[Edge] = []
    repo_id = _repo_id(repo)

    summary = extraction.summary if extraction else ""
    nodes.append(
        Node(
            "Repo",
            repo_id,
            {
                "path": repo.path,
                "remote": repo.remote,
                "name": repo.name,
                "last_commit_date": repo.last_commit_date,
                "summary": summary,
                "language": str(docs.metadata.get("primaryLanguage", "")),
            },
        )
    )

    learned_at = repo.last_commit_date or ""
    _add_doc_facts(nodes, edges, repo, repo_id, docs, learned_at)
    if extraction is not None:
        _add_extraction(nodes, edges, repo, repo_id, extraction, learned_at)
    return nodes, edges


def _add_doc_facts(
    nodes: list[Node],
    edges: list[Edge],
    repo: DiscoveredRepo,
    repo_id: str,
    docs: RepoDocs,
    learned_at: str,
) -> None:
    for source, text in (("readme", docs.readme), ("claude_md", docs.claude_md)):
        if not text.strip():
            continue
        fid = _fact_id(repo, source, 0)
        nodes.append(
            Node(
                "Fact",
                fid,
                {"text": _clip(text), "learned_at": learned_at, "source": source},
            )
        )
        edges.append(Edge("relates_to", "Fact", fid, "Repo", repo_id))


def _add_extraction(
    nodes: list[Node],
    edges: list[Edge],
    repo: DiscoveredRepo,
    repo_id: str,
    extraction: Extraction,
    learned_at: str,
) -> None:
    for owner in extraction.owners:
        pid = _person_id(owner)
        nodes.append(Node("Person", pid, {"name": owner}))
        edges.append(Edge("authored_by", "Repo", repo_id, "Person", pid))

    extraction_texts = _extraction_texts(extraction)
    for seq, text in enumerate(extraction_texts):
        fid = _fact_id(repo, "extraction", seq)
        nodes.append(
            Node(
                "Fact",
                fid,
                {"text": _clip(text), "learned_at": learned_at, "source": "extraction"},
            )
        )
        edges.append(Edge("relates_to", "Fact", fid, "Repo", repo_id))


def _extraction_texts(extraction: Extraction) -> list[str]:
    texts: list[str] = []
    if extraction.purpose:
        texts.append(f"Purpose: {extraction.purpose}")
    if extraction.components:
        texts.append(f"Components: {', '.join(extraction.components)}")
    if extraction.stack:
        texts.append(f"Stack: {', '.join(extraction.stack)}")
    return texts


def map_and_remember(
    store: MemoryStore,
    repo: DiscoveredRepo,
    docs: RepoDocs,
    extraction: Extraction | None,
) -> int:
    """Build and persist the graph for one repo. Returns node count written."""
    nodes, edges = build_graph(repo, docs, extraction)
    store.remember(nodes=nodes, edges=edges)
    return len(nodes)
