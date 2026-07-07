"""Fetch: read local docs for a repo (spec 08 §Fetch).

README.md + CLAUDE.md (repo root AND ``.claude/CLAUDE.md``) are read from the
LOCAL clone (fast, offline). ``gh`` metadata is best-effort and injected as a
callable so tests never hit the network. Returns RAW (un-redacted) texts; the
redact choke-point runs downstream.
"""

from __future__ import annotations

from pathlib import Path

from edith.ingest.fetch import RepoDocs, fetch_repo_docs


def test_reads_readme_and_both_claude_md_locations(tmp_path: Path) -> None:
    repo = tmp_path / "portal"
    (repo / ".claude").mkdir(parents=True)
    (repo / "README.md").write_text("# Portal\nOnboarding portal.")
    (repo / "CLAUDE.md").write_text("Repo rules here.")
    (repo / ".claude" / "CLAUDE.md").write_text("Nested claude rules.")

    docs = fetch_repo_docs(str(repo))

    assert isinstance(docs, RepoDocs)
    assert docs.readme == "# Portal\nOnboarding portal."
    assert "Repo rules here." in docs.claude_md
    assert "Nested claude rules." in docs.claude_md


def test_missing_files_yield_empty_strings(tmp_path: Path) -> None:
    repo = tmp_path / "bare"
    repo.mkdir()

    docs = fetch_repo_docs(str(repo))

    assert docs.readme == ""
    assert docs.claude_md == ""
    assert docs.metadata == {}


def test_gh_metadata_injected_and_best_effort(tmp_path: Path) -> None:
    repo = tmp_path / "svc"
    repo.mkdir()
    (repo / "README.md").write_text("hi")

    def fake_gh(name: str) -> dict[str, object]:
        assert name == "svc"
        return {"description": "a service", "primaryLanguage": "Python", "topics": ["x"]}

    docs = fetch_repo_docs(str(repo), gh_metadata=fake_gh)

    assert docs.metadata["description"] == "a service"
    assert docs.metadata["primaryLanguage"] == "Python"


def test_gh_failure_does_not_raise(tmp_path: Path) -> None:
    repo = tmp_path / "svc"
    repo.mkdir()

    def boom(_name: str) -> dict[str, object]:
        raise RuntimeError("gh not installed")

    docs = fetch_repo_docs(str(repo), gh_metadata=boom)

    assert docs.metadata == {}
