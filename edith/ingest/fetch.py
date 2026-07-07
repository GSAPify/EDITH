"""Fetch a repo's docs from the local clone (spec 08 §Fetch).

Reads README.md and CLAUDE.md (repo root + ``.claude/CLAUDE.md``) straight off
disk — fast and offline. ``gh`` metadata (description / language / topics) is a
best-effort enrichment injected as a callable so it is unit-tested without the
network and never fails the pipeline.

Returns RAW (un-redacted) text. Redaction is a downstream choke-point
(``redact.py``); doing it here would let a future caller bypass it.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

# Injectable metadata provider: repo name -> flat metadata dict.
GhMetadata = Callable[[str], dict[str, object]]


@dataclass(frozen=True)
class RepoDocs:
    """Raw docs + metadata read from a local clone (before redaction)."""

    name: str
    path: str
    readme: str
    claude_md: str
    metadata: dict[str, object] = field(default_factory=dict)


def fetch_repo_docs(
    repo_path: str | Path,
    gh_metadata: GhMetadata | None = None,
) -> RepoDocs:
    """Read local docs for the clone at ``repo_path``.

    ``claude_md`` concatenates the repo-root ``CLAUDE.md`` and ``.claude/CLAUDE.md``
    (either may be absent). ``gh_metadata`` defaults to the real ``gh`` fetch and
    is best-effort: any failure yields empty metadata, never an exception.
    """
    repo = Path(repo_path).expanduser()
    name = repo.name
    readme = _read_text(repo / "README.md")
    claude_parts = [
        _read_text(repo / "CLAUDE.md"),
        _read_text(repo / ".claude" / "CLAUDE.md"),
    ]
    claude_md = "\n\n".join(part for part in claude_parts if part)

    provider = gh_metadata if gh_metadata is not None else _gh_metadata
    metadata = _safe_metadata(provider, name)

    return RepoDocs(
        name=name, path=str(repo), readme=readme, claude_md=claude_md, metadata=metadata
    )


def _read_text(path: Path) -> str:
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def _safe_metadata(provider: GhMetadata, name: str) -> dict[str, object]:
    try:
        return provider(name)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
            FileNotFoundError, json.JSONDecodeError, RuntimeError):
        return {}


def _gh_metadata(name: str) -> dict[str, object]:
    """Best-effort ``gh repo view`` for description / language / topics."""
    result = subprocess.run(
        [
            "gh", "repo", "view", f"patterninc/{name}",
            "--json", "description,primaryLanguage,repositoryTopics",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    raw = json.loads(result.stdout)
    lang = raw.get("primaryLanguage") or {}
    topics = raw.get("repositoryTopics") or []
    return {
        "description": raw.get("description") or "",
        "primaryLanguage": lang.get("name", "") if isinstance(lang, dict) else "",
        "topics": [t.get("name", "") for t in topics if isinstance(t, dict)],
    }
