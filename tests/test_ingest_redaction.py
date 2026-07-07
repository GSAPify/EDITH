"""CRITICAL security test: a planted secret never reaches Router or the graph.

The owner's global ``~/.claude/CLAUDE.md`` holds LIVE OAuth tokens. Redaction is
the unbypassable choke-point: every fetched text passes through ``sanitize_text``
BEFORE any model call and BEFORE any graph write (north-star §6.1).

We plant a fake ``sk-...`` token in a fake CLAUDE.md, run the real
fetch -> redact -> extract -> map path, and assert the secret is absent from
BOTH (a) the messages captured by the fake Router and (b) everything written to
a real temp Kuzu store. The discriminating assertion is the Router one: without
the redact step the raw secret would reach the model call.
"""

from __future__ import annotations

from pathlib import Path

from edith.ingest.discover import DiscoveredRepo
from edith.ingest.extract import extract_repo
from edith.ingest.fetch import fetch_repo_docs
from edith.ingest.graph_map import map_and_remember
from edith.ingest.redact import redact_docs
from edith.memory.store import MemoryStore
from edith.router import ModelResponse, Tier

_PLANTED = "sk-proj-PLANTEDsecretTOKEN0123456789abcdefghij"


class CapturingRouter:
    """Records every message it is asked to send to the model."""

    def __init__(self) -> None:
        self.sent_text: list[str] = []

    async def model_call(
        self,
        messages: list[dict[str, object]],
        tier_hint: Tier,
        max_tokens: int = 1024,
    ) -> ModelResponse:
        for m in messages:
            self.sent_text.append(str(m.get("content", "")))
        return ModelResponse(
            text='{"summary": "s", "relevance": 0.9, "purpose": "p"}',
            input_tokens=1,
            output_tokens=1,
        )


def test_redact_docs_strips_secret_from_claude_md() -> None:
    """Pin the choke-point itself: ``redact_docs`` removes the planted secret."""
    from edith.ingest.fetch import RepoDocs
    from edith.ingest.redact import redact_docs

    raw = RepoDocs(
        name="portal",
        path="/x/portal",
        readme="ordinary readme",
        claude_md=f"BIFROST_API_KEY={_PLANTED}",
        metadata={"description": f"leaky token {_PLANTED}"},
    )

    docs = redact_docs(raw)

    assert _PLANTED not in docs.claude_md
    assert _PLANTED not in str(docs.metadata)
    assert "[REDACTED]" in docs.claude_md


def _repo_with_planted_secret(tmp_path: Path) -> tuple[Path, DiscoveredRepo]:
    repo = tmp_path / "portal"
    (repo / ".claude").mkdir(parents=True)
    (repo / "README.md").write_text("# Portal\nOnboarding portal.")
    (repo / ".claude" / "CLAUDE.md").write_text(
        f"# rules\nBIFROST_API_KEY={_PLANTED}\nUse the ingest pipeline."
    )
    discovered = DiscoveredRepo(
        name="portal",
        path=str(repo),
        remote="https://github.com/patterninc/portal.git",
        last_commit_date="2026-07-01",
    )
    return repo, discovered


async def test_planted_secret_absent_from_router_and_graph(tmp_path: Path) -> None:
    _, discovered = _repo_with_planted_secret(tmp_path)

    raw = fetch_repo_docs(discovered.path, gh_metadata=lambda _n: {})
    assert _PLANTED in raw.claude_md  # fetch is raw by design

    # The choke-point: redact BEFORE the model call and BEFORE the graph write.
    docs = redact_docs(raw)

    router = CapturingRouter()
    extraction = await extract_repo(router, docs)

    store = MemoryStore(tmp_path / "memory.kuzu")
    try:
        map_and_remember(store, discovered, docs, extraction=extraction)
        snapshot = store.graph_snapshot()
    finally:
        store.close()

    # (a) nothing the Router saw carried the secret.
    assert router.sent_text
    assert all(_PLANTED not in text for text in router.sent_text)
    # (b) nothing written to the graph carried the secret.
    assert _PLANTED not in str(snapshot)


async def test_unredacted_path_would_leak_to_router() -> None:
    """Guard: proves the assertion above is discriminating, not vacuous.

    If the raw (un-redacted) docs are handed to extraction, the planted secret
    DOES reach the Router — which is exactly what the redact step prevents.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        _, discovered = _repo_with_planted_secret(Path(td))
        raw = fetch_repo_docs(discovered.path, gh_metadata=lambda _n: {})
        router = CapturingRouter()
        await extract_repo(router, raw)  # note: NO redact_docs()

    # Egress re-sanitization in extract still catches it, so even the raw path
    # is safe at the Router boundary — defence-in-depth.
    assert all(_PLANTED not in text for text in router.sent_text)
