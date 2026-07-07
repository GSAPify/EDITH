"""Extraction: Sonnet classify + budget-aware Opus deep pass (spec 08).

Router is constructor-injected; a deterministic fake captures every ``model_call``
so we assert tier routing and the budget skip WITHOUT a live call.
"""

from __future__ import annotations

from edith.ingest.extract import extract_repo
from edith.ingest.fetch import RepoDocs
from edith.router import ModelResponse, Tier


class FakeRouter:
    """Deterministic Router double. Scripts responses per tier; records calls."""

    def __init__(self, summary_json: str, deep_json: str = "{}") -> None:
        self._summary_json = summary_json
        self._deep_json = deep_json
        self.calls: list[dict[str, object]] = []

    async def model_call(
        self,
        messages: list[dict[str, object]],
        tier_hint: Tier,
        max_tokens: int = 1024,
    ) -> ModelResponse:
        self.calls.append(
            {"tier": tier_hint, "messages": messages, "max_tokens": max_tokens}
        )
        text = self._summary_json if tier_hint is Tier.SONNET else self._deep_json
        return ModelResponse(text=text, input_tokens=1, output_tokens=1)


def _docs() -> RepoDocs:
    return RepoDocs(
        name="portal", path="/x/portal", readme="Onboarding portal.", claude_md=""
    )


async def test_relevant_repo_runs_sonnet_then_opus() -> None:
    router = FakeRouter(
        summary_json='{"summary": "onboarding portal", "relevance": 0.9}',
        deep_json='{"purpose": "onboard brands", "project": "Brand Onboarding", '
        '"components": ["api"], "stack": ["python"], "owners": ["Akhil"]}',
    )

    result = await extract_repo(router, _docs())

    tiers = [c["tier"] for c in router.calls]
    assert tiers == [Tier.SONNET, Tier.OPUS]
    assert result.deep is True
    assert result.summary == "onboarding portal"
    assert result.purpose == "onboard brands"
    assert result.project == "Brand Onboarding"
    assert result.stack == ["python"]
    assert result.owners == ["Akhil"]


async def test_low_relevance_skips_opus() -> None:
    router = FakeRouter(summary_json='{"summary": "misc", "relevance": 0.1}')

    result = await extract_repo(router, _docs())

    tiers = [c["tier"] for c in router.calls]
    assert tiers == [Tier.SONNET]  # budget: no Opus
    assert result.deep is False
    assert result.relevance == 0.1


async def test_tolerates_prose_wrapped_json() -> None:
    router = FakeRouter(
        summary_json='Sure! {"summary": "s", "relevance": 0.5} hope that helps',
        deep_json='```json\n{"purpose": "p"}\n```',
    )

    result = await extract_repo(router, _docs())

    assert result.summary == "s"
    assert result.purpose == "p"


async def test_opus_max_tokens_is_configurable() -> None:
    router = FakeRouter(summary_json='{"summary": "s", "relevance": 1.0}')

    await extract_repo(router, _docs(), deep_max_tokens=64)

    opus_call = next(c for c in router.calls if c["tier"] is Tier.OPUS)
    assert opus_call["max_tokens"] == 64
