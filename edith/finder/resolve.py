"""Real-time resolve-on-miss (spec 09 §Resolve-on-miss).

When EDITH is asked about a repo NOT already in the graph, ``resolve_repo``:

  a. **HIT**   — the repo node ``repo-<name>`` already exists -> return it.
                 No fetch, no model call.
  b. **MISS**  — locate it: a local ``~/gitstuff/<name>`` patterninc clone
                 (read via ``edith.ingest.fetch``), else a best-effort ``gh``
                 README fetch. Then REDACT everything (the same unbypassable
                 choke-point as ingest — north-star §6.1) and:
                   - FAST: a Sonnet answer NOW from the redacted docs (low
                     latency = EDITH's voice, spec 05 §Routing philosophy).
                   - BACKGROUND: an awaitable that runs the Opus deep-extract
                     (reusing ``ingest.extract``/``graph_map``) and ``remember``s
                     it, so the NEXT mention is an instant graph hit. The caller
                     runs it via ``asyncio.create_task`` (Slice-5 ``think_async``
                     will formalize this seam — see spec 09).
  c. **NOT_FOUND** — nowhere -> a clean result. No model call, no task.

Redaction runs ONCE at the fetch boundary (``redact_docs``), so BOTH the fast
answer and the background extract inherit clean docs; a planted secret reaches
neither the Router nor the store.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from edith.ingest.discover import DiscoveredRepo
from edith.ingest.extract import RouterLike, extract_repo
from edith.ingest.fetch import RepoDocs, fetch_repo_docs
from edith.ingest.graph_map import map_and_remember
from edith.ingest.redact import redact_docs
from edith.memory.secrets import sanitize_text
from edith.memory.store import MemoryStore
from edith.router import Tier

# gh README fetcher: repo name -> raw README text ("" when absent).
GhReadme = Callable[[str], str]

_DEFAULT_SCAN_ROOT = "~/gitstuff"
_FAST_MAX_TOKENS = 256
_DEEP_MAX_TOKENS = 512

_FAST_SYSTEM = (
    "You are EDITH. The owner just asked about a repo you had not yet learned. "
    "Using ONLY the redacted docs below, tell them in one or two sentences what "
    "the repo is and does. If the docs are thin, say so plainly."
)


class ResolveStatus(Enum):
    """Outcome of a resolve attempt."""

    HIT = "hit"              # already in the graph
    RESOLVED = "resolved"    # was a miss; fetched + answered fast
    NOT_FOUND = "not_found"  # nowhere to be found


@dataclass
class ResolveResult:
    """Result of ``resolve_repo``.

    ``background`` is an awaitable running the Opus deep-extract + ``remember``
    for the RESOLVED path (``None`` for HIT / NOT_FOUND). The caller schedules it
    with ``asyncio.create_task`` and does NOT block the fast answer on it.
    """

    status: ResolveStatus
    name: str
    answer: str
    background: Coroutine[Any, Any, None] | None = None


async def resolve_repo(
    name: str,
    store: MemoryStore,
    router: RouterLike,
    *,
    scan_root: str | Path = _DEFAULT_SCAN_ROOT,
    gh_readme: GhReadme | None = None,
    deep_max_tokens: int = _DEEP_MAX_TOKENS,
) -> ResolveResult:
    """Resolve ``name`` against the graph, then a local clone, then ``gh``."""
    # a. HIT — exact node lookup for the deterministic repo id.
    if _repo_in_graph(store, name):
        return ResolveResult(ResolveStatus.HIT, name=name, answer="", background=None)

    # b. MISS — locate the repo's docs (local clone first, then gh README).
    located = _locate(name, scan_root, gh_readme)
    if located is None:
        # c. NOT_FOUND — nowhere. Clean result, no model call, no task.
        return ResolveResult(ResolveStatus.NOT_FOUND, name=name, answer="", background=None)

    discovered, raw = located
    # REDACT at the boundary: both the fast answer and the background extract
    # read from this ONE redacted copy (north-star §6.1, unbypassable).
    docs = redact_docs(raw)

    # FAST: a Sonnet answer NOW (low latency = EDITH's voice).
    answer = await _fast_answer(router, docs)

    # BACKGROUND: Opus deep-extract + remember, so the next mention is a hit.
    background = _deep_extract(store, router, discovered, docs, deep_max_tokens)

    return ResolveResult(ResolveStatus.RESOLVED, name=name, answer=answer, background=background)


def _repo_in_graph(store: MemoryStore, name: str) -> bool:
    """True when a ``repo-<name>`` node already exists (deterministic id from
    ``graph_map._repo_id``). Exact lookup — not semantic recall."""
    repo_id = f"repo-{name}"
    rows = store._rows(  # noqa: SLF001 - internal read for the exact hit-check
        "MATCH (r:Repo {id: $id}) RETURN r.id", {"id": repo_id}
    )
    return any(True for _ in rows)


def _locate(
    name: str, scan_root: str | Path, gh_readme: GhReadme | None
) -> tuple[DiscoveredRepo, RepoDocs] | None:
    """Find the repo's docs. Local clone first; else a gh README fetch.

    Returns ``(discovered, raw_docs)`` on success or ``None`` when the repo
    cannot be located anywhere.
    """
    root = Path(scan_root).expanduser()
    local = root / name
    if (local / ".git").is_dir():
        discovered = DiscoveredRepo(
            name=name,
            path=str(local),
            remote=f"github.com/patterninc/{name}",
            last_commit_date="",
        )
        raw = fetch_repo_docs(local, gh_metadata=lambda _n: {})
        return discovered, raw

    provider = gh_readme if gh_readme is not None else _gh_readme
    readme = _safe_gh_readme(provider, name)
    if not readme.strip():
        return None

    discovered = DiscoveredRepo(
        name=name,
        path="",
        remote=f"github.com/patterninc/{name}",
        last_commit_date="",
    )
    raw = RepoDocs(name=name, path="", readme=readme, claude_md="", metadata={})
    return discovered, raw


async def _fast_answer(router: RouterLike, docs: RepoDocs) -> str:
    """One Sonnet call over the redacted docs — the low-latency voice answer.

    ``extract._call`` re-sanitizes at the model egress as defence-in-depth; here
    we build the message the same way so the fast path shares that guarantee.
    """
    blob = f"# repo: {docs.name}\n\n{docs.readme}\n\n{docs.claude_md}".strip()
    content = sanitize_text(f"{_FAST_SYSTEM}\n\n{blob}")
    messages: list[dict[str, object]] = [{"role": "user", "content": content}]
    response = await router.model_call(messages, Tier.SONNET, max_tokens=_FAST_MAX_TOKENS)
    return response.text


async def _deep_extract(
    store: MemoryStore,
    router: RouterLike,
    discovered: DiscoveredRepo,
    docs: RepoDocs,
    deep_max_tokens: int,
) -> None:
    """Background Opus deep-extract of the ALREADY-redacted docs -> remember.

    Reuses the ingest extract + graph-map path so the next mention of this repo
    is an instant graph (and, on a ``VectorMemoryStore``, semantic) hit. Runs off
    the fast answer's critical path — the caller schedules it with
    ``asyncio.create_task``. Slice-5 ``think_async`` will formalize this seam.
    """
    extraction = await extract_repo(router, docs, deep_max_tokens=deep_max_tokens)
    map_and_remember(store, discovered, docs, extraction)


def _safe_gh_readme(provider: GhReadme, name: str) -> str:
    try:
        return provider(name)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
            FileNotFoundError, json.JSONDecodeError, RuntimeError):
        return ""


def _gh_readme(name: str) -> str:
    """Best-effort ``gh api`` README fetch for a patterninc repo ("" on miss)."""
    result = subprocess.run(
        ["gh", "api", f"repos/patterninc/{name}/readme",
         "--jq", ".content", "-H", "Accept: application/vnd.github.raw+json"],
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    return result.stdout
