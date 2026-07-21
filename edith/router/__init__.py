"""Router — EDITH's model-call gateway over the Bifrost adapter (spec 05).

Every model call in EDITH routes through ``model_call(messages, tier_hint)``.
This slice ships the single-tier passthrough the north-star (§7) permits until
Slice 5 adds the two-call latency-masking mechanics: the tier hint maps straight
to a Bifrost model id and one HTTP call is made. The surface stays stable so
Slice 5 can replace the internals without touching callers.
"""

from edith.router.background import supervised_reason, think_async
from edith.router.bifrost import MODEL_CALL_ERRORS, ModelChunk, ModelResponse, Router
from edith.router.tiers import TaskType, Tier, TierDecision, resolve_tier

__all__ = [
    "MODEL_CALL_ERRORS",
    "ModelChunk",
    "ModelResponse",
    "Router",
    "TaskType",
    "Tier",
    "TierDecision",
    "resolve_tier",
    "supervised_reason",
    "think_async",
]
