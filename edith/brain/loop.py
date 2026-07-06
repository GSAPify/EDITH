"""The Brain orchestrator loop.

One pass on a ``voice.utterance`` (spec 01 §"The core loop"):

  1. RECALL     — Memory.recall(utterance) pulls the relevant slice of the graph.
  2. ASSEMBLE   — system preamble + recalled facts + the utterance -> messages.
  3. REDACT     — sanitize_text over every message (never-persist / §6.1) so a
                  credential never reaches Router / Bifrost.
  4. DECIDE     — Router.model_call(messages, tier) for the answer (single-tier
                  passthrough; the Guard authorize/budget gates are later work).
  5. REMEMBER   — write the exchange (utterance + answer) back to Memory; the
                  never-persist filter runs again inside remember().
  6. PUBLISH    — brain.decision {intent, action, tier_used}.

COMPACT (step 5 in the spec) is deferred — it needs Session/Conversation node
tables and a token-counted working-context buffer that don't exist yet
(tracked in the Completion Record). Memory is called synchronously; it is a
blocking Kuzu store and there is no real concurrency in this slice. When that
changes, wrap the two Memory calls in ``asyncio.to_thread``.
"""

from __future__ import annotations

import time
from typing import Protocol

from edith.bus import Event, EventBus
from edith.memory.secrets import sanitize_text
from edith.memory.store import Node
from edith.router import ModelResponse, Tier

_SYSTEM_PREAMBLE = (
    "You are EDITH, the owner's always-on local assistant. Use the recalled "
    "facts below to answer without asking the owner to re-explain context."
)

# Passthrough default tier for a plain answer (north-star §6.2: cheapest that
# fits; Slice 5 gives the Router the final say and the two-call mechanics).
_DEFAULT_TIER = Tier.SONNET


class MemoryLike(Protocol):
    """The slice of the Memory contract Brain uses (north-star §4.3)."""

    def recall(self, query: str) -> list[dict[str, object]]: ...

    def remember(
        self, nodes: list[Node] | None = None, edges: list[object] | None = None
    ) -> None: ...


class RouterLike(Protocol):
    """The slice of the Router contract Brain uses (spec 05 §4.3)."""

    async def model_call(
        self,
        messages: list[dict[str, object]],
        tier_hint: Tier,
        max_tokens: int = ...,
    ) -> ModelResponse: ...


class Brain:
    """The orchestrator loop; subscribes itself to ``voice.utterance``."""

    def __init__(self, bus: EventBus, memory: MemoryLike, router: RouterLike) -> None:
        self._bus = bus
        self._memory = memory
        self._router = router
        bus.subscribe("voice.utterance", self._on_utterance)

    async def _on_utterance(self, event: Event) -> None:
        utterance = str(event.payload.get("text", ""))

        # 1. RECALL
        recalled = self._memory.recall(utterance)

        # 2. ASSEMBLE + 3. REDACT (sanitize every message before it leaves the box)
        messages = _assemble(utterance, recalled)
        safe_messages = _redact(messages)

        # 4. DECIDE (single-tier passthrough)
        response = await self._router.model_call(safe_messages, _DEFAULT_TIER)

        # 5. REMEMBER the exchange (never-persist filter runs again inside remember)
        self._remember_exchange(utterance, response.text)

        # 6. PUBLISH the decision
        await self._bus.publish(
            "brain.decision",
            source="brain",
            payload={
                "intent": "answer_query",
                "action": "answer",
                "tier_used": _DEFAULT_TIER.value,
                "answer": response.text,
            },
        )

    def _remember_exchange(self, utterance: str, answer: str) -> None:
        ts = str(time.time())
        turn_id = f"conv-{ts}"
        # Redact FIRST (never-persist, §6.1). MemoryStore.remember sanitizes too,
        # but Brain must not depend on the store for the secrets boundary — the
        # exchange text is redacted here so it is safe regardless of the backend.
        text = sanitize_text(f"owner asked: {utterance} | EDITH answered: {answer}")
        node = Node(label="Fact", id=turn_id, props={"text": text, "learned_at": ts})
        self._memory.remember(nodes=[node])


def _assemble(utterance: str, recalled: list[dict[str, object]]) -> list[dict[str, object]]:
    """Working context = system preamble + recalled facts + the utterance."""
    facts = "\n".join(
        f"- {hit.get('text')}" for hit in recalled if hit.get("text")
    )
    system = _SYSTEM_PREAMBLE
    if facts:
        system += "\n\nRecalled facts:\n" + facts
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": utterance},
    ]


def _redact(messages: list[dict[str, object]]) -> list[dict[str, object]]:
    """Run the never-persist filter over every message's content (§6.1)."""
    redacted: list[dict[str, object]] = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            redacted.append({**message, "content": sanitize_text(content)})
        else:
            redacted.append(message)
    return redacted
