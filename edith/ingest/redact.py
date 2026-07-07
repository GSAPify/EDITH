"""Redaction choke-point for ingestion (spec 08 §Redaction-first).

Every fetched text passes through here BEFORE any model call and BEFORE any
graph write (north-star §6.1). The owner's global ``~/.claude/CLAUDE.md`` holds
LIVE OAuth tokens; a planted or real secret must never reach Bifrost or the Kuzu
DB. This is a thin, deliberately un-bypassable wrapper over ``sanitize_text`` so
the whole pipeline has one obvious place to point at for "where do secrets die".

``sanitize_text`` is idempotent, so re-running redaction at the model-call and
graph-write egress points is safe defence-in-depth, not double-work.
"""

from __future__ import annotations

from dataclasses import replace

from edith.ingest.fetch import RepoDocs
from edith.memory.secrets import sanitize_text


def redact_docs(docs: RepoDocs) -> RepoDocs:
    """Return ``docs`` with every free-text field run through the secrets filter.

    README and CLAUDE.md are redacted; the string values in ``metadata`` too.
    Structural fields (name, path) are left as-is.
    """
    metadata = {
        k: (sanitize_text(v) if isinstance(v, str) else v)
        for k, v in docs.metadata.items()
    }
    return replace(
        docs,
        readme=sanitize_text(docs.readme),
        claude_md=sanitize_text(docs.claude_md),
        metadata=metadata,
    )
