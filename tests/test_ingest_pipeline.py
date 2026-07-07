"""Pipeline orchestration (spec 08 §Pipeline).

discover -> fetch -> REDACT -> extract -> map/remember -> status report.
Everything is injected (scan root, router, gh) so no network / no live model.
``--dry-run`` does discover+fetch+redact+map preview with NO model call and NO
write. Incremental: a repo already ingested at the same last_commit_date is
skipped.
"""

from __future__ import annotations

from pathlib import Path

import edith.ingest.pipeline as pipeline_mod
from edith.ingest.pipeline import IngestReport, run_ingest
from edith.memory.store import MemoryStore
from edith.router import ModelResponse, Tier


class FakeRouter:
    def __init__(self) -> None:
        self.calls = 0

    async def model_call(
        self,
        messages: list[dict[str, object]],
        tier_hint: Tier,
        max_tokens: int = 1024,
    ) -> ModelResponse:
        self.calls += 1
        text = (
            '{"summary": "svc", "relevance": 0.9}'
            if tier_hint is Tier.SONNET
            else '{"purpose": "do things", "owners": ["Akhil"]}'
        )
        return ModelResponse(text=text, input_tokens=1, output_tokens=1)


def _make_clone(root: Path, name: str) -> None:
    repo = root / name
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "config").write_text(
        f'[remote "origin"]\n\turl = https://github.com/patterninc/{name}.git\n'
    )
    (repo / "README.md").write_text(f"# {name}\nA patterninc service.")


async def test_full_run_writes_nodes_and_reports(tmp_path: Path) -> None:
    scan = tmp_path / "gitstuff"
    scan.mkdir()
    _make_clone(scan, "portal")
    _make_clone(scan, "sheriff")
    data_dir = tmp_path / "data"

    router = FakeRouter()
    report = await run_ingest(
        scan_root=scan,
        data_dir=data_dir,
        router=router,
        gh_metadata=lambda _n: {},
        include_global=False,
    )

    assert isinstance(report, IngestReport)
    assert report.repos_ingested == 2
    assert report.facts_written > 0
    assert router.calls > 0
    store = MemoryStore(data_dir / "memory.kuzu")
    try:
        assert store.count("Repo") == 2
    finally:
        store.close()


async def test_dry_run_makes_no_model_calls_and_no_writes(tmp_path: Path) -> None:
    scan = tmp_path / "gitstuff"
    scan.mkdir()
    _make_clone(scan, "portal")
    data_dir = tmp_path / "data"

    router = FakeRouter()
    report = await run_ingest(
        scan_root=scan,
        data_dir=data_dir,
        router=router,
        gh_metadata=lambda _n: {},
        dry_run=True,
        include_global=False,
    )

    assert router.calls == 0
    assert not (data_dir / "memory.kuzu").exists()
    assert report.repos_ingested == 1  # previewed
    assert report.dry_run is True


async def test_repos_filter_limits_targets(tmp_path: Path) -> None:
    scan = tmp_path / "gitstuff"
    scan.mkdir()
    _make_clone(scan, "portal")
    _make_clone(scan, "sheriff")
    data_dir = tmp_path / "data"

    report = await run_ingest(
        scan_root=scan,
        data_dir=data_dir,
        router=FakeRouter(),
        gh_metadata=lambda _n: {},
        repos=["portal"],
        include_global=False,
    )

    assert report.repos_ingested == 1
    assert report.summaries[0].name == "portal"


async def test_incremental_skips_unchanged_repo(tmp_path: Path) -> None:
    scan = tmp_path / "gitstuff"
    scan.mkdir()
    _make_clone(scan, "portal")
    data_dir = tmp_path / "data"
    common = {
        "scan_root": scan,
        "data_dir": data_dir,
        "gh_metadata": lambda _n: {},
        "include_global": False,
    }

    first = await run_ingest(router=FakeRouter(), **common)
    assert first.repos_ingested == 1

    second = await run_ingest(router=FakeRouter(), **common)
    assert second.repos_ingested == 0
    assert second.repos_skipped == 1


async def test_status_report_never_prints_secrets(tmp_path: Path) -> None:
    scan = tmp_path / "gitstuff"
    scan.mkdir()
    repo = scan / "portal"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "config").write_text(
        '[remote "origin"]\n\turl = https://github.com/patterninc/portal.git\n'
    )
    (repo / "README.md").write_text("# portal\nBIFROST_API_KEY=sk-proj-LEAKYsecret123456")
    data_dir = tmp_path / "data"

    report = await run_ingest(
        scan_root=scan, data_dir=data_dir, router=FakeRouter(),
        gh_metadata=lambda _n: {}, include_global=False,
    )
    text = report.render()

    assert "sk-proj-LEAKYsecret123456" not in text


async def test_global_claude_md_ingested_as_redacted_owner_facts(
    tmp_path: Path, monkeypatch
) -> None:
    scan = tmp_path / "gitstuff"
    scan.mkdir()  # no repos — isolate the global-file behaviour
    data_dir = tmp_path / "data"

    fake_global = tmp_path / "CLAUDE.md"
    fake_global.write_text(
        "# Owner prefs\nNever add Claude as a co-author.\n\n"
        "client_secret: GOCSPX-PLANTEDfakeSecretValue\n\nPrefer small PRs."
    )
    monkeypatch.setattr(pipeline_mod, "_GLOBAL_CLAUDE_MD", fake_global)

    report = await run_ingest(
        scan_root=scan, data_dir=data_dir, router=FakeRouter(),
        gh_metadata=lambda _n: {}, include_global=True,
    )

    assert report.facts_written > 0
    store = MemoryStore(data_dir / "memory.kuzu")
    try:
        snapshot = store.graph_snapshot()
    finally:
        store.close()
    text = str(snapshot)
    assert "GOCSPX-PLANTEDfakeSecretValue" not in text  # redacted hard
    assert "co-author" in text  # the non-secret fact survives
