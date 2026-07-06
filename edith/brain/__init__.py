"""Brain — the orchestrator decision loop (spec 01 §"The core loop").

Brain is a bus subscriber. On a ``voice.utterance`` event it runs one pass of
the core loop and publishes ``brain.decision``. This slice ships the
single-tier passthrough the north-star (§7) permits: recall -> assemble ->
redact -> Router.model_call -> remember -> publish. Two-call latency masking,
the Guard autonomy/budget gates, compaction and skill dispatch are later work.
"""

from edith.brain.loop import Brain

__all__ = ["Brain"]
