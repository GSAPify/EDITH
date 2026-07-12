"""Discover target repos from local ``patterninc`` clones (spec 08 §Discovery).

Ground truth is the local clone, not a GitHub author query: the owner's Pattern
commit identity differs from their ``gh`` login, so ``author=<login>`` returns
0 commits. Instead we scan a root (default ``~/gitstuff``) for git working
copies whose ``origin`` remote points at the ``patterninc`` org. Fully offline;
``gh`` metadata enrichment is a later, best-effort step (``fetch.py``).
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

# origin url pointing at the patterninc org, https or ssh:
#   https://github.com/patterninc/<repo>[.git]
#   git@github.com:patterninc/<repo>[.git]
_PATTERNINC_REMOTE = re.compile(r"github\.com[:/]patterninc/", re.IGNORECASE)
_ORIGIN_URL = re.compile(
    r'\[remote "origin"\][^\[]*?\burl\s*=\s*(\S+)', re.IGNORECASE | re.DOTALL
)


@dataclass(frozen=True)
class DiscoveredRepo:
    """A repo to ingest — a local clone, or a metadata-only entry from the org API.

    ``org`` is the GitHub org/workspace it belongs to. Defaults to ``patterninc``
    (the incumbent org, whose node ids stay unprefixed for back-compat with the
    existing graph + ``resolve.py``); other orgs get org-scoped ids so a repo name
    shared across orgs can't collide. ``path`` is empty for metadata-only entries.
    """

    name: str
    path: str
    remote: str
    last_commit_date: str
    org: str = "patterninc"


def discover_repos(scan_root: str | Path) -> list[DiscoveredRepo]:
    """Return patterninc clones under ``scan_root``, sorted by name.

    A directory qualifies if it holds a ``.git/config`` whose ``origin`` url
    matches the patterninc org. ``last_commit_date`` is read from git (best
    effort; empty string if git is unavailable or the repo has no commits).
    """
    root = Path(scan_root).expanduser()
    if not root.is_dir():
        return []

    found: list[DiscoveredRepo] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        remote = _origin_remote(entry)
        if remote is None or not _PATTERNINC_REMOTE.search(remote):
            continue
        found.append(
            DiscoveredRepo(
                name=entry.name,
                path=str(entry),
                remote=remote,
                last_commit_date=_last_commit_date(entry),
            )
        )
    return found


def _origin_remote(repo: Path) -> str | None:
    config = repo / ".git" / "config"
    if not config.is_file():
        return None
    match = _ORIGIN_URL.search(config.read_text())
    return match.group(1) if match else None


def _last_commit_date(repo: Path) -> str:
    """ISO-8601 date of the last commit, or "" if unavailable."""
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%cs"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return ""
    return result.stdout.strip()
