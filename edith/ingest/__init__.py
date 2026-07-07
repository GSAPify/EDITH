"""Repo-knowledge ingestion pipeline (spec 08).

Populates EDITH's live Memory graph from the owner's local ``patterninc`` clones:
discover repos -> fetch docs -> REDACT -> classify/extract (Sonnet/Opus) ->
map to nodes/edges -> remember. Redaction is the unbypassable choke-point: every
fetched text passes through ``sanitize_text`` BEFORE any model call and BEFORE
any graph write (north-star §6.1).
"""

from edith.ingest.discover import DiscoveredRepo, discover_repos
from edith.ingest.pipeline import IngestReport, run_ingest

__all__ = ["DiscoveredRepo", "IngestReport", "discover_repos", "run_ingest"]
