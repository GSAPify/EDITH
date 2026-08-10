"""Tier selection logic (spec 05 §Tier selection) — pure, no I/O.

The Router calls ``resolve_tier`` to pick the cheapest model tier that fits, from the caller's
``tier_hint`` plus a couple of override rules. Routing philosophy (revised Session 3,
latency-first):

- **Haiku** — instant: acks, wake confirm, narration, trivial lookups.
- **Sonnet** — the DEFAULT live voice for every conversational turn.
- **Opus** — explicitly-invoked or background deep work; NEVER silently blocks a live turn.

So a deep/hard turn with no explicit opus hint does not promote to opus inline — it stays on
Sonnet (which holds the turn) and sets ``suggest_background`` so Brain can fire a background
opus job (``think_async``, deferred to a follow-up). An *explicit* ``OPUS`` hint is honored
(owner asked for depth), gated by the budget check.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum


class Tier(Enum):
    """Model size class the Router selects (spec 05 §Tier selection).

    Defined here (the tier-selection module) so ``bifrost.py`` can import it without a
    cycle; re-exported from ``edith.router`` so callers are unaffected.
    """

    HAIKU = "haiku"
    SONNET = "sonnet"
    OPUS = "opus"


# HAIKU handles short work; above this token count it can't do the job → promote to Sonnet.
HAIKU_MAX_TOKENS = 500
# Beyond this, even a hint-less turn is "deep" → Sonnet holds the turn, opus goes background.
DEEP_TOKENS = 4000


class TaskType(Enum):
    """Coarse task class Brain can pass to bias tier selection."""

    GENERAL = "general"          # a normal conversational turn → Sonnet
    LOOKUP = "lookup"            # cheap fact lookup → Haiku when small
    ACK_FILLER = "ack_filler"    # spoken filler ("let me look…") → always Haiku
    CODE_REVIEW = "code_review"  # deep signals → Sonnet live + background opus
    PLAN = "plan"
    DEBATE = "debate"
    DEEP_ANALYSIS = "deep_analysis"


_DEEP_TASKS = frozenset(
    {TaskType.CODE_REVIEW, TaskType.PLAN, TaskType.DEBATE, TaskType.DEEP_ANALYSIS}
)
_CHEAP_TASKS = frozenset({TaskType.LOOKUP, TaskType.ACK_FILLER})


@dataclass(frozen=True)
class TierDecision:
    """The resolved tier plus flags the caller acts on."""

    tier: Tier
    budget_limited: bool = False  # opus was wanted but denied → fell back to sonnet
    suggest_background: bool = False  # deep work; live stays sonnet, fire background opus


def estimate_tokens(messages: list[dict[str, object]]) -> int:
    """Rough token count (~chars/4) for the override heuristics. Not billing-accurate."""
    chars = 0
    for m in messages:
        content = m.get("content")
        if isinstance(content, str):
            chars += len(content)
        elif isinstance(content, list):
            chars += sum(len(b.get("text", "")) for b in content if isinstance(b, dict))
    return chars // 4


def resolve_tier(
    tier_hint: Tier | None,
    *,
    task_type: TaskType = TaskType.GENERAL,
    token_count: int = 0,
    budget_allows_opus: bool = True,
) -> TierDecision:
    """Pick the tier for this call. See module docstring for the philosophy."""
    # Demotion wins over everything: a spoken filler is always the cheapest tier.
    if task_type is TaskType.ACK_FILLER:
        return TierDecision(Tier.HAIKU)

    if tier_hint is Tier.HAIKU:
        # Haiku can't handle a large payload → promote to Sonnet.
        return TierDecision(Tier.SONNET if token_count > HAIKU_MAX_TOKENS else Tier.HAIKU)

    if tier_hint is Tier.SONNET:
        return TierDecision(Tier.SONNET)

    if tier_hint is Tier.OPUS:
        # Explicit opus is honored, but budget-gated (Guard's real budget is deferred;
        # Router injects a check that defaults to allow).
        if budget_allows_opus:
            return TierDecision(Tier.OPUS)
        return TierDecision(Tier.SONNET, budget_limited=True)

    # tier_hint is None → default heuristic.
    if token_count <= HAIKU_MAX_TOKENS and task_type in _CHEAP_TASKS:
        return TierDecision(Tier.HAIKU)
    if task_type in _DEEP_TASKS or token_count > DEEP_TOKENS:
        # Latency-first: never block the live turn on opus. Sonnet answers now; the deep
        # work is flagged for a background opus job (think_async — deferred follow-up).
        return TierDecision(Tier.SONNET, suggest_background=True)
    return TierDecision(Tier.SONNET)


# Tier → Bifrost model id. ONE map, read by every entry point.
#
# Previously duplicated in edith/daemon/__main__.py and edith/voice/__main__.py, with a
# TODO(config) flagging the drift risk — which then bit: the two entry points had to be
# updated in lockstep on every model bump, and nothing enforced it. Env vars still override
# per tier, so a model can be pinned without a code change.
#
# Model ids carry NO date suffix: `claude-opus-5`, not `claude-opus-5-20260401`. The
# gateway resolves the alias to whatever dated snapshot is current.
_DEFAULT_MODELS: dict[Tier, str] = {
    Tier.HAIKU: "claude-haiku-4-5",
    Tier.SONNET: "claude-sonnet-5",  # the live talking voice — every conversational turn
    Tier.OPUS: "claude-opus-5",  # explicit depth + background reasoning
}

_MODEL_ENV_VARS: dict[Tier, str] = {
    Tier.HAIKU: "BIFROST_MODEL_HAIKU",
    Tier.SONNET: "BIFROST_MODEL_SONNET",
    Tier.OPUS: "BIFROST_MODEL_OPUS",
}


def resolve_models() -> dict[Tier, str]:
    """The tier→model map, with per-tier ``BIFROST_MODEL_*`` env overrides applied."""
    return {
        tier: os.environ.get(_MODEL_ENV_VARS[tier], default)
        for tier, default in _DEFAULT_MODELS.items()
    }
