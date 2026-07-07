"""Discovery: find target repos from local ``patterninc`` clones (spec 08).

Ground truth is the local clone's ``origin`` remote, NOT a GitHub author query
(the owner's Pattern commit identity differs from their gh login). We scan a
scan-root for git repos whose ``origin`` points at ``github.com[:/]patterninc/``.
No network: fake ``.git/config`` files in a temp dir.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from edith.ingest.discover import DiscoveredRepo, discover_repos


def _make_repo(root: Path, name: str, remote: str | None) -> Path:
    repo = root / name
    (repo / ".git").mkdir(parents=True)
    if remote is not None:
        (repo / ".git" / "config").write_text(
            '[remote "origin"]\n'
            f"\turl = {remote}\n"
            "\tfetch = +refs/heads/*:refs/remotes/origin/*\n"
        )
    return repo


def test_finds_patterninc_https_and_ssh_remotes(tmp_path: Path) -> None:
    _make_repo(tmp_path, "portal", "https://github.com/patterninc/portal.git")
    _make_repo(tmp_path, "sheriff", "git@github.com:patterninc/sheriff.git")

    found = discover_repos(tmp_path)

    names = {r.name for r in found}
    assert names == {"portal", "sheriff"}
    assert all(isinstance(r, DiscoveredRepo) for r in found)
    portal = next(r for r in found if r.name == "portal")
    assert portal.remote == "https://github.com/patterninc/portal.git"
    assert portal.path == str(tmp_path / "portal")


def test_ignores_non_patterninc_and_non_git_dirs(tmp_path: Path) -> None:
    _make_repo(tmp_path, "mine", "git@github.com:someoneelse/mine.git")
    _make_repo(tmp_path, "noremote", None)
    (tmp_path / "plain-dir").mkdir()

    found = discover_repos(tmp_path)

    assert found == []


def test_missing_scan_root_returns_empty(tmp_path: Path) -> None:
    assert discover_repos(tmp_path / "does-not-exist") == []


def test_last_commit_date_read_from_real_git_repo(tmp_path: Path) -> None:
    repo = tmp_path / "real"
    repo.mkdir()
    env = {"GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "T",
           "GIT_COMMITTER_EMAIL": "t@x"}
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/patterninc/real.git"],
        cwd=repo, check=True,
    )
    (repo / "f.txt").write_text("hi")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.name=T", "-c", "user.email=t@x", "commit", "-q", "-m", "init"],
        cwd=repo, check=True, env={**env, "PATH": _path()},
    )

    found = discover_repos(tmp_path)

    assert len(found) == 1
    assert found[0].last_commit_date  # non-empty ISO-ish date string


def _path() -> str:
    import os

    return os.environ.get("PATH", "/usr/bin:/bin")
