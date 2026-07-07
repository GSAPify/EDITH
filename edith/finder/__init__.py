"""NL repo finder + real-time resolve-on-miss (spec 09).

``find_repos`` does semantic + graph search over the ingested Memory graph and
ranks the owner's repos by relevance. ``resolve_repo`` handles the miss path:
when EDITH is asked about a repo NOT in the graph, it locates it (local clone or
``gh``), redacts, answers fast with Sonnet, and schedules an Opus deep-extract
in the background so the next mention is an instant graph hit.

Redaction is the same unbypassable choke-point as ingest (north-star §6.1): every
fetched text passes through ``sanitize_text`` BEFORE any model call or write.
"""

from edith.finder.finder import RepoHit, find_repos, summarize_hits
from edith.finder.resolve import ResolveResult, ResolveStatus, resolve_repo

__all__ = [
    "RepoHit",
    "ResolveResult",
    "ResolveStatus",
    "find_repos",
    "resolve_repo",
    "summarize_hits",
]
