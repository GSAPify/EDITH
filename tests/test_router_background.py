"""Router background-reasoning methods (spec 12).

Headless: a fake router that duck-types ``Router.model_call`` — records each
``(messages, tier)`` and returns canned responses. No httpx, no sleeps, no network.

- ``supervised_reason`` (SYNCHRONOUS, awaited): a fast draft then a review pass that
  critiques+improves it. The load-bearing property is that the review call is *prompted
  with the draft* (draft text appears in the review payload) and the REFINED text is
  returned — not a blind re-answer.
- ``think_async`` (BACKGROUND): schedules an opus ``asyncio.Task``; when it completes it
  awaits ``on_result`` if set. The returned task yields the response either way.
"""

from __future__ import annotations

import asyncio

import pytest

from edith.router import ModelResponse, Tier, supervised_reason, think_async

pytestmark = pytest.mark.asyncio


class _FakeRouter:
    """Duck-types ``Router.model_call``. Records calls; returns queued responses in order."""

    def __init__(self, responses: list[ModelResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[list[dict[str, object]], Tier, int]] = []

    async def model_call(
        self,
        messages: list[dict[str, object]],
        tier_hint: Tier,
        max_tokens: int = 1024,
    ) -> ModelResponse:
        self.calls.append((messages, tier_hint, max_tokens))
        return self._responses.pop(0)


def _resp(text: str) -> ModelResponse:
    return ModelResponse(text=text, input_tokens=1, output_tokens=1)


async def test_supervised_reason_makes_draft_then_review_at_right_tiers() -> None:
    router = _FakeRouter([_resp("rough draft"), _resp("polished refined answer")])

    result = await supervised_reason(
        router, [{"role": "user", "content": "explain X"}]
    )

    assert len(router.calls) == 2
    draft_call, review_call = router.calls
    assert draft_call[1] is Tier.SONNET  # draft on the fast tier
    assert review_call[1] is Tier.OPUS  # review on the strong tier
    # The refined (second) response is returned, not the draft.
    assert result.text == "polished refined answer"


async def test_supervised_reason_review_is_prompted_with_the_draft() -> None:
    # The discriminator: the review pass must SEE the draft, else it just re-answers blind.
    router = _FakeRouter([_resp("DRAFT-TOKEN-42"), _resp("refined")])

    await supervised_reason(router, [{"role": "user", "content": "q"}])

    review_messages = router.calls[1][0]
    blob = "".join(str(m.get("content", "")) for m in review_messages)
    assert "DRAFT-TOKEN-42" in blob  # the draft text reached the review payload
    # The original user turn is still present too (review has full context).
    assert "q" in blob


async def test_supervised_reason_honors_custom_tiers_and_max_tokens() -> None:
    router = _FakeRouter([_resp("d"), _resp("r")])

    await supervised_reason(
        router,
        [{"role": "user", "content": "q"}],
        draft_tier=Tier.HAIKU,
        review_tier=Tier.SONNET,
        max_tokens=256,
    )

    assert router.calls[0][1] is Tier.HAIKU
    assert router.calls[1][1] is Tier.SONNET
    assert router.calls[0][2] == 256
    assert router.calls[1][2] == 256


async def test_think_async_returns_task_that_yields_response() -> None:
    router = _FakeRouter([_resp("deep result")])

    task = await think_async(router, [{"role": "user", "content": "think about Y"}])

    assert isinstance(task, asyncio.Task)
    result = await task
    assert result.text == "deep result"
    assert router.calls[0][1] is Tier.OPUS  # background work runs on opus


async def test_think_async_awaits_on_result_when_set() -> None:
    router = _FakeRouter([_resp("deep result")])
    received: list[ModelResponse] = []

    async def on_result(resp: ModelResponse) -> None:
        received.append(resp)

    task = await think_async(
        router, [{"role": "user", "content": "q"}], on_result=on_result
    )
    result = await task

    assert received == [result]  # on_result got the same response the task yields


async def test_think_async_without_consumer_still_runs_and_result_retrievable() -> None:
    # HONESTY: default on_result=None has NO production consumer. The task still runs and
    # the result is retrievable via the returned task — but nobody SPEAKS it yet.
    router = _FakeRouter([_resp("unheard answer")])

    task = await think_async(router, [{"role": "user", "content": "q"}])
    result = await task

    assert result.text == "unheard answer"
    assert len(router.calls) == 1  # the background call did fire
