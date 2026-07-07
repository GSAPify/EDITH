"""Ingestion pipeline orchestration (spec 08 §Pipeline).

Ties the slice together for one run::

    discover -> [per repo] fetch -> REDACT -> extract -> map/remember -> report

Redaction is the choke-point: ``redact_docs`` runs before extraction (the model
call) AND before ``map_and_remember`` (the graph write). Bounded concurrency
across repos; incremental — a repo already ingested at the same
``last_commit_date`` is skipped. ``dry_run`` does discover+fetch+redact+map
PREVIEW with no model call and no write.

The Router and the ``gh`` metadata provider are injected so the whole pipeline
is unit-tested with deterministic doubles — no network, no live model.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from edith.ingest.discover import DiscoveredRepo, discover_repos
from edith.ingest.extract import Extraction, RouterLike, extract_repo
from edith.ingest.fetch import GhMetadata, fetch_repo_docs
from edith.ingest.graph_map import build_graph
from edith.ingest.redact import redact_docs
from edith.memory.secrets import sanitize_text
from edith.memory.store import Edge, MemoryStore, Node

_DEFAULT_SCAN_ROOT = "~/gitstuff"
_DEFAULT_CONCURRENCY = 4
_DEFAULT_DEEP_MAX_TOKENS = 512


@dataclass(frozen=True)
class RepoSummary:
    """One-line per-repo outcome for the status report."""

    name: str
    facts: int
    relevance: float
    deep: bool
    status: str  # "ingested" | "skipped" | "previewed"
    note: str = ""


@dataclass
class IngestReport:
    """End-of-run status report (printed to stdout)."""

    dry_run: bool = False
    repos_ingested: int = 0
    repos_skipped: int = 0
    facts_written: int = 0
    data_dir: str = ""
    summaries: list[RepoSummary] = field(default_factory=list)

    def render(self) -> str:
        """Human-readable report. Run through the secrets filter as a last line
        of defence — stdout must never carry a credential (north-star §6.1)."""
        header = "DRY RUN (no model calls, no writes)" if self.dry_run else "INGEST"
        lines = [
            f"EDITH repo ingest — {header}",
            f"  data dir:        {self.data_dir}",
            f"  repos ingested:  {self.repos_ingested}",
            f"  repos skipped:   {self.repos_skipped}",
            f"  facts written:   {self.facts_written}",
            "  per-repo:",
        ]
        for s in self.summaries:
            tier = "opus" if s.deep else "sonnet-only"
            note = f" — {s.note}" if s.note else ""
            lines.append(
                f"    - {s.name}: {s.status}, {s.facts} facts, "
                f"relevance={s.relevance:.2f} ({tier}){note}"
            )
        return sanitize_text("\n".join(lines))


def _existing_commit_dates(store: MemoryStore) -> dict[str, str]:
    """Map repo name -> last_commit_date already in the store (for skip check)."""
    dates: dict[str, str] = {}
    for name, date in store._rows(  # noqa: SLF001 - internal read for incremental skip
        "MATCH (r:Repo) RETURN r.name, r.last_commit_date"
    ):
        if name is not None:
            dates[str(name)] = str(date or "")
    return dates


async def run_ingest(
    *,
    scan_root: str | Path = _DEFAULT_SCAN_ROOT,
    data_dir: str | Path,
    router: RouterLike,
    gh_metadata: GhMetadata | None = None,
    repos: list[str] | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    concurrency: int = _DEFAULT_CONCURRENCY,
    deep_max_tokens: int = _DEFAULT_DEEP_MAX_TOKENS,
    include_global: bool = True,
) -> IngestReport:
    """Run the ingestion pipeline once and return the status report."""
    targets = discover_repos(scan_root)
    if repos:
        wanted = set(repos)
        targets = [t for t in targets if t.name in wanted]
    if limit is not None:
        targets = targets[:limit]

    report = IngestReport(dry_run=dry_run, data_dir=str(Path(data_dir).expanduser()))

    store: MemoryStore | None = None
    existing: dict[str, str] = {}
    if not dry_run:
        db_path = Path(data_dir).expanduser() / "memory.kuzu"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        store = MemoryStore(db_path)
        existing = _existing_commit_dates(store)

    try:
        semaphore = asyncio.Semaphore(max(1, concurrency))
        results = await asyncio.gather(
            *(
                _process_repo(
                    repo, router, gh_metadata, dry_run, existing, semaphore,
                    deep_max_tokens,
                )
                for repo in targets
            )
        )
        for repo, summary, nodes, edges, extraction in results:
            _record(report, store, dry_run, repo, summary, nodes, edges, extraction)
        if include_global:
            _ingest_global_claude_md(report, store, dry_run)
    finally:
        if store is not None:
            store.close()
    return report


_GLOBAL_CLAUDE_MD = Path("~/.claude/CLAUDE.md")
_OWNER_PERSON_ID = "person-owner"
_GLOBAL_FACT_CHARS = 500


def _ingest_global_claude_md(
    report: IngestReport, store: MemoryStore | None, dry_run: bool
) -> None:
    """Ingest the owner's GLOBAL ``~/.claude/CLAUDE.md`` as owner-context Facts.

    Redacted HARD before anything: this file holds LIVE OAuth tokens. Chunked
    into a few Facts (source ``claude_md``) attached to the owner Person node so
    the graph carries owner context, never the credentials themselves.
    """
    path = _GLOBAL_CLAUDE_MD.expanduser()
    if not path.is_file():
        return
    redacted = sanitize_text(path.read_text(encoding="utf-8", errors="replace"))
    chunks = _chunk(redacted, _GLOBAL_FACT_CHARS)
    if not chunks:
        return

    nodes: list[Node] = [Node("Person", _OWNER_PERSON_ID, {"name": "Owner (global context)"})]
    edges: list[Edge] = []
    for i, chunk in enumerate(chunks):
        digest = hashlib.sha1(f"global:{i}".encode()).hexdigest()[:12]  # noqa: S324
        fid = f"fact-global-{digest}"
        nodes.append(
            Node("Fact", fid, {"text": chunk, "learned_at": "", "source": "claude_md"})
        )
        edges.append(Edge("relates_to", "Fact", fid, "Person", _OWNER_PERSON_ID))

    fact_count = len(chunks)
    report.facts_written += fact_count
    status = "previewed" if dry_run else "ingested"
    report.summaries.append(
        RepoSummary("~/.claude/CLAUDE.md", fact_count, 0.0, False, status, "owner context")
    )
    if not dry_run and store is not None:
        store.remember(nodes=nodes, edges=edges)


def _chunk(text: str, size: int) -> list[str]:
    """Split into paragraph-ish chunks no longer than ``size`` chars."""
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks: list[str] = []
    for para in paragraphs:
        chunks.append(para[:size].rstrip() + ("…" if len(para) > size else ""))
    return chunks


async def _process_repo(
    repo: DiscoveredRepo,
    router: RouterLike,
    gh_metadata: GhMetadata | None,
    dry_run: bool,
    existing: dict[str, str],
    semaphore: asyncio.Semaphore,
    deep_max_tokens: int,
) -> tuple[DiscoveredRepo, str, list[Node], list[Edge], Extraction | None]:
    """Fetch+redact+(extract) one repo -> (repo, outcome, nodes, edges, extraction)."""
    async with semaphore:
        if not dry_run and existing.get(repo.name) == repo.last_commit_date:
            return repo, "skipped", [], [], None

        raw = await asyncio.to_thread(fetch_repo_docs, repo.path, gh_metadata)
        docs = redact_docs(raw)  # CHOKE-POINT: before model call and before write.

        extraction: Extraction | None = None
        if not dry_run:
            extraction = await extract_repo(router, docs, deep_max_tokens=deep_max_tokens)

        nodes, edges = build_graph(repo, docs, extraction)
        return repo, "ok", nodes, edges, extraction


def _record(
    report: IngestReport,
    store: MemoryStore | None,
    dry_run: bool,
    repo: DiscoveredRepo,
    outcome: str,
    nodes: list[Node],
    edges: list[Edge],
    extraction: Extraction | None,
) -> None:
    if outcome == "skipped":
        report.repos_skipped += 1
        report.summaries.append(
            RepoSummary(repo.name, 0, 0.0, False, "skipped", "unchanged last_commit_date")
        )
        return

    fact_count = sum(1 for n in nodes if n.label == "Fact")
    if dry_run:
        report.repos_ingested += 1
        report.facts_written += fact_count
        report.summaries.append(
            RepoSummary(repo.name, fact_count, 0.0, False, "previewed")
        )
        return

    assert store is not None  # noqa: S101 - non-dry-run always has a store
    store.remember(nodes=nodes, edges=edges)
    report.repos_ingested += 1
    report.facts_written += fact_count
    relevance = extraction.relevance if extraction else 0.0
    deep = extraction.deep if extraction else False
    report.summaries.append(
        RepoSummary(repo.name, fact_count, relevance, deep, "ingested")
    )
