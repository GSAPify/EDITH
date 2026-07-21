"""Background / two-agent reasoning over the Router (spec 12).

The north-star "two agents, fast masks slow" philosophy, made concrete as two SEPARATE
model calls — never one inference, never shared weights.

- ``supervised_reason`` (SYNCHRONOUS, awaited): a fast draft then a strong review pass that
  critiques+improves it, returning the REFINED response. Clear consumer: Brain's deep-query
  path. Fully built + headless-tested here.
- ``think_async`` (BACKGROUND): schedules an opus ``asyncio.Task``; on completion it awaits
  ``on_result`` if set. ⚠ NO production consumer yet — a background answer arriving ~20 s later
  must be spoken through the voice half-duplex gate + cooldown + conversation-window +
  echo-suppression, an unsolved interaction the lead wires separately. Default ``on_result=None``
  (task still runs, result retrievable). This is the seam, not the finished feature.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from edith.router.bifrost import _DEFAULT_MAX_TOKENS, ModelResponse, Router
from edith.router.tiers import Tier

# Router-owned instruction for the review pass. The reviewer gets the full original context
# plus the draft (folded in as the assistant turn) and is asked to critique+improve it —
# it refines the draft, it does not re-answer blind.
_REVIEW_INSTRUCTION = (
    "Critique the draft answer above and produce an improved, final version. "
    "Fix any errors, fill gaps, and tighten it. Return only the improved answer."
)


async def supervised_reason(
    router: Router,
    messages: list[dict[str, object]],
    *,
    draft_tier: Tier = Tier.SONNET,
    review_tier: Tier = Tier.OPUS,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
) -> ModelResponse:
    """Draft-then-review: a fast draft, then a strong critique+improve pass.

    Two separate ``model_call``s (fast masks slow, spec 05). The draft is folded into the
    review payload as an assistant turn so the reviewer refines it rather than re-answering
    blind. Returns the REFINED response (the second call).
    """
    draft = await router.model_call(messages, draft_tier, max_tokens)
    review_messages: list[dict[str, object]] = [
        *messages,
        {"role": "assistant", "content": draft.text},
        {"role": "user", "content": _REVIEW_INSTRUCTION},
    ]
    return await router.model_call(review_messages, review_tier, max_tokens)


async def think_async(
    router: Router,
    messages: list[dict[str, object]],
    *,
    on_result: Callable[[ModelResponse], Awaitable[None]] | None = None,
    tier: Tier = Tier.OPUS,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
) -> asyncio.Task[ModelResponse]:
    """Schedule a BACKGROUND opus ``model_call`` as a task; await ``on_result`` on completion.

    Returns the task immediately (caller may await the result). ⚠ UNFINISHED as a feature:
    ``on_result`` has no production consumer yet — speaking a ~20 s-late answer must pass the
    voice half-duplex gate + cooldown + conversation-window + echo-suppression, which the lead
    wires separately. The default ``on_result=None`` is NOT a consumer; the task still runs and
    the result stays retrievable via the returned task.
    """

    async def _run() -> ModelResponse:
        response = await router.model_call(messages, tier, max_tokens)
        if on_result is not None:
            await on_result(response)
        return response

    # Returned (not orphaned), so the caller owns its lifetime — no RUF006 fire-and-forget here.
    return asyncio.create_task(_run())
