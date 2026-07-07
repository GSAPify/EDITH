"""Real-time resolve-on-miss (spec 09 §Resolve-on-miss).

``resolve_repo(name, store, router, ...)``:
  a. HIT   — repo already in the graph -> return it, no fetch, no model call.
  b. MISS  — locate it (local ~/gitstuff clone, else gh README) -> REDACT ->
             fast Sonnet answer NOW + schedule background Opus deep-extract.
  c. NOT_FOUND — nowhere -> clean result, no model call, no task.

Redaction is the unbypassable choke-point (north-star §6.1): a planted secret in
the fetched docs must reach NEITHER the fake Router NOR the store. Everything is
injected (fake Router, temp store, a fake local-repo dir, a fake gh README
fetcher) so no network / no live model.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from edith.finder import ResolveResult, ResolveStatus, resolve_repo
from edith.memory.store import Node
from edith.memory.vector import VectorMemoryStore
from edith.router import ModelResponse, Tier


class FakeRouter:
    def __init__(self) -> None:
        self.calls: list[tuple[list[dict[str, object]], Tier]] = []
        self.seen_content: list[str] = []

    async def model_call(
        self, messages: list[dict[str, object]], tier_hint: Tier, max_tokens: int = 1024
    ) -> ModelResponse:
        self.calls.append((messages, tier_hint))
        for m in messages:
            content = m.get("content")
            if isinstance(content, str):
                self.seen_content.append(content)
        if tier_hint is Tier.SONNET:
            return ModelResponse(
                text="widget is a patterninc service for widgets.",
                input_tokens=1, output_tokens=1,
            )
        return ModelResponse(
            text='{"purpose": "widgets", "owners": ["Akhil"]}',
            input_tokens=1, output_tokens=1,
        )


async def test_hit_returns_from_memory_no_fetch_no_model(tmp_path: Path) -> None:
    store = VectorMemoryStore(tmp_path / "memory.kuzu")
    router = FakeRouter()
    fetched: list[str] = []
    try:
        store.remember(
            nodes=[Node("Repo", "repo-widget", {"name": "widget", "summary": "widget svc"})]
        )
        result = await resolve_repo(
            "widget", store, router,
            scan_root=tmp_path / "nope",
            gh_readme=lambda _n: fetched.append(_n) or "",  # type: ignore[func-returns-value]
        )
    finally:
        store.close()

    assert result.status is ResolveStatus.HIT
    assert result.name == "widget"
    assert router.calls == []          # no model call on a hit
    assert fetched == []               # no fetch on a hit
    assert result.background is None   # nothing to schedule


async def test_miss_local_clone_fast_answer_and_schedules_background(tmp_path: Path) -> None:
    scan = tmp_path / "gitstuff"
    repo = scan / "widget"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "config").write_text(
        '[remote "origin"]\n\turl = https://github.com/patterninc/widget.git\n'
    )
    (repo / "README.md").write_text("# widget\nA patterninc widgets service.")

    store = VectorMemoryStore(tmp_path / "memory.kuzu")
    router = FakeRouter()
    try:
        result = await resolve_repo(
            "widget", store, router,
            scan_root=scan,
            gh_readme=lambda _n: "",  # local clone found -> gh not used
        )
        assert result.status is ResolveStatus.RESOLVED
        assert result.answer                       # a fast Sonnet answer NOW
        assert result.background is not None        # background extract scheduled
        # fast path used exactly one (Sonnet) call; background not yet run
        assert len(router.calls) == 1
        assert router.calls[0][1] is Tier.SONNET

        # running the background awaitable does the Opus deep-extract + remember
        await result.background
        assert store.count("Repo") == 1
    finally:
        store.close()


async def test_miss_gh_readme_when_no_local_clone(tmp_path: Path) -> None:
    scan = tmp_path / "gitstuff"
    scan.mkdir()  # empty — no local clone, forces gh path
    store = VectorMemoryStore(tmp_path / "memory.kuzu")
    router = FakeRouter()
    calls: list[str] = []
    try:
        result = await resolve_repo(
            "widget", store, router,
            scan_root=scan,
            gh_readme=lambda n: calls.append(n) or "# widget\nfrom gh readme",  # type: ignore[func-returns-value]
        )
        assert result.status is ResolveStatus.RESOLVED
        assert calls == ["widget"]     # gh README fetched
        assert result.answer
        if result.background is not None:
            await result.background
    finally:
        store.close()


async def test_not_found_returns_cleanly_no_model_no_task(tmp_path: Path) -> None:
    scan = tmp_path / "gitstuff"
    scan.mkdir()  # empty
    store = VectorMemoryStore(tmp_path / "memory.kuzu")
    router = FakeRouter()
    try:
        result = await resolve_repo(
            "ghost-repo", store, router,
            scan_root=scan,
            gh_readme=lambda _n: "",  # gh finds nothing either
        )
    finally:
        store.close()

    assert result.status is ResolveStatus.NOT_FOUND
    assert router.calls == []          # no model call
    assert result.background is None   # no task scheduled
    assert isinstance(result, ResolveResult)


async def test_planted_secret_never_reaches_router_or_store(tmp_path: Path) -> None:
    """The unbypassable redaction boundary: a secret in fetched docs reaches
    NEITHER the fake Router NOR the store — fast path and background alike."""
    scan = tmp_path / "gitstuff"
    repo = scan / "widget"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "config").write_text(
        '[remote "origin"]\n\turl = https://github.com/patterninc/widget.git\n'
    )
    secret = "GOCSPX-PLANTED_resolve_secret_9999"
    (repo / "README.md").write_text(f"# widget\nclient_secret: {secret}\nA service.")

    store = VectorMemoryStore(tmp_path / "memory.kuzu")
    router = FakeRouter()
    try:
        result = await resolve_repo(
            "widget", store, router, scan_root=scan, gh_readme=lambda _n: "",
        )
        assert result.background is not None
        await result.background  # run the background extract + remember too

        # 1. never reached the Router (fast Sonnet OR background Opus)
        assert all(secret not in c for c in router.seen_content)
        # 2. never reached the graph/vector store
        snapshot = str(store.graph_snapshot())
        assert secret not in snapshot
    finally:
        store.close()


def test_resolve_result_is_awaitable_friendly() -> None:
    """A HIT/NOT_FOUND result carries no background task -> safe to ignore."""
    result = ResolveResult(status=ResolveStatus.NOT_FOUND, name="x", answer="", background=None)
    assert result.background is None
    assert not asyncio.iscoroutine(result.background)


def test_gh_readme_uses_raw_accept_and_no_jq(monkeypatch) -> None:  # noqa: ANN001
    """Regression: the raw-content Accept header returns README markdown on
    stdout, so ``--jq`` must NOT be used (jq would try to parse markdown as JSON
    and fail — that made every gh-path resolve NOT_FOUND). Lock the arg shape and
    that stdout is returned verbatim."""
    from edith.finder import resolve as resolve_mod

    captured: dict[str, object] = {}

    class _Result:
        stdout = "# README\n\nadczar analytics."

    def fake_run(args, **kwargs):  # noqa: ANN001, ANN003
        captured["args"] = args
        return _Result()

    monkeypatch.setattr(resolve_mod.subprocess, "run", fake_run)

    readme = resolve_mod._gh_readme("adczar")

    args = captured["args"]
    assert "--jq" not in args  # the bug: --jq on a raw (non-JSON) response
    assert "Accept: application/vnd.github.raw+json" in args
    assert "repos/patterninc/adczar/readme" in args
    assert readme == "# README\n\nadczar analytics."  # stdout returned verbatim
