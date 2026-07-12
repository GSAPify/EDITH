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

PAUSE (spec 01 §"Pause + Memory"): Brain reads a zero-arg ``is_paused``
predicate wired from the daemon's RuntimeState. While paused it skips the whole
pass — no model_call AND no remember — the privacy-respecting reading of a
manual pause. Default is not-paused so Brain works standalone.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Protocol

from edith.bus import Event, EventBus
from edith.finder import ResolveResult, ResolveStatus
from edith.memory.secrets import sanitize_text
from edith.memory.store import Node
from edith.router import ModelResponse, Tier
from edith.skills import Skill, SkillContext

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


# A resolve-on-miss callable (spec 09): name -> ResolveResult. Injected so Brain
# stays decoupled from the finder's fetch/model machinery; the daemon wires the
# real ``functools.partial(resolve_repo, store=..., router=...)``.
ResolveRepoLike = Callable[[str], Awaitable[ResolveResult]]

# A repo mention looks like a hyphen/underscore token or an explicit "<name> repo"
# phrase. Deliberately a thin heuristic, NOT an NLP layer (spec 09 §Open questions):
# it only fires the resolver, which itself no-ops cleanly on a not-found name.
_REPO_PHRASE = re.compile(r"\b([A-Za-z0-9][\w-]{2,})\s+repo\b", re.IGNORECASE)


class Brain:
    """The orchestrator loop; subscribes itself to ``voice.utterance``."""

    def __init__(
        self,
        bus: EventBus,
        memory: MemoryLike,
        router: RouterLike,
        is_paused: Callable[[], bool] = lambda: False,
        resolve_repo: ResolveRepoLike | None = None,
        skills: Sequence[Skill] | None = None,
    ) -> None:
        self._bus = bus
        self._memory = memory
        self._router = router
        # Skill registry (spec 02). Default None -> empty list, so a Brain with
        # no skills behaves exactly as the pre-skill loop (existing tests green),
        # mirroring the resolve_repo=None no-op pattern.
        self._skills = list(skills or [])
        # Zero-arg predicate wired from the daemon's RuntimeState. Default
        # not-paused so Brain used standalone (and the existing tests) behave
        # exactly as before.
        self._is_paused = is_paused
        # Resolve-on-miss hook (spec 09). Default None -> no-op, so a recall
        # miss proceeds straight to the model exactly as the pre-hook Brain
        # (keeps the existing tests green).
        self._resolve_repo = resolve_repo
        bus.subscribe("voice.utterance", self._on_utterance)

    async def _on_utterance(self, event: Event) -> None:
        # Pause semantics (spec 01 §"Pause + Memory"): while paused, skip the
        # model call AND the remember — the privacy-respecting reading of a
        # manual pause ("don't capture this moment"). The in-RAM conversation
        # buffer is retained simply by dropping nothing here.
        if self._is_paused():
            return

        utterance = str(event.payload.get("text", ""))

        # 0. DISPATCH (spec 02): the first skill whose trigger is a substring of
        # the utterance owns this turn — run it, publish skill.result, and skip
        # the recall→answer path. No match -> fall through to the answer loop.
        if await self._dispatch_skill(utterance):
            return

        # 1. RECALL
        recalled = self._memory.recall(utterance)

        # 1b. RESOLVE-ON-MISS (spec 09): recall came back empty AND the utterance
        # names a repo AND a resolver is wired -> fetch+redact+fast-answer the
        # unknown repo NOW, and let its background deep-extract run so the next
        # mention is an instant graph hit. Folded into the recalled context so the
        # model answers with it. No resolver / no repo mention -> unchanged.
        resolved_answer = await self._resolve_on_miss(utterance, recalled)
        if resolved_answer:
            recalled = [*recalled, {"text": resolved_answer}]

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

    async def _dispatch_skill(self, utterance: str) -> bool:
        """Run the first skill whose trigger matches; publish its result.

        Returns True when a skill handled the turn (caller then returns), False
        when nothing matched (caller falls through to the recall→answer path).
        """
        lowered = utterance.lower()
        for skill in self._skills:
            if not any(trigger.lower() in lowered for trigger in skill.triggers):
                continue
            result = await skill.run(SkillContext(utterance=utterance, memory=self._memory))
            # A skill may DECLINE a turn its trigger matched (handled=False) — e.g.
            # desktop's broad "open "/"play " caught an utterance it can't action. Skip
            # it (no publish) and keep looking; nothing matches -> fall through to the
            # recall→answer loop instead of dead-ending the turn.
            if not result.handled:
                continue
            await self._bus.publish(
                "skill.result",
                source=skill.name,
                payload={
                    "findings": result.findings,
                    "pr_url": result.pr_url,
                    "posted": result.posted,
                    "remembered": result.remembered,
                    "asked": result.asked,
                },
            )
            return True
        return False

    async def _resolve_on_miss(
        self, utterance: str, recalled: list[dict[str, object]]
    ) -> str:
        """If recall missed and the utterance names a repo, resolve it now.

        Returns the fast answer text to fold into context, or "" when nothing was
        resolved. The background deep-extract (RESOLVED path) is scheduled with
        ``asyncio.create_task`` so it never blocks this turn (spec 09; Slice-5
        ``think_async`` will formalize the seam).
        """
        if self._resolve_repo is None or recalled:
            return ""
        match = _REPO_PHRASE.search(utterance)
        if match is None:
            return ""

        result = await self._resolve_repo(match.group(1))
        if result.status is ResolveStatus.RESOLVED:
            if result.background is not None:
                asyncio.create_task(result.background)  # noqa: RUF006 - fire-and-forget seam
            return result.answer
        return ""

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
