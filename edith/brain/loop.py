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

from edith.brain.history import TurnBuffer
from edith.bus import Event, EventBus
from edith.finder import ResolveResult, ResolveStatus
from edith.memory.secrets import sanitize_text
from edith.memory.store import Edge, Node
from edith.router import MODEL_CALL_ERRORS, BackgroundJob, ModelResponse, Tier
from edith.router.tiers import DEEP_TOKENS, estimate_tokens
from edith.skills import Skill, SkillContext

_SYSTEM_PREAMBLE = (
    "You are EDITH, the owner's always-on local assistant. Use the recalled "
    "facts below to answer without asking the owner to re-explain context."
)

# Passthrough default tier for a plain answer (north-star §6.2: cheapest that
# fits; Slice 5 gives the Router the final say and the two-call mechanics).
_DEFAULT_TIER = Tier.SONNET

# Spoken when the router's transport fails (spec 10 §Model-error seam) — the daemon
# has no other handler on this path, so Brain speaks an apology instead of going silent.
_MODEL_ERROR_REPLY = "Sorry sir, I couldn't reach the model just now."

# Explicit background-reasoning trigger (spec 11): the IMPERATIVE "think about X" / "think on
# X". A thin heuristic mirroring _REPO_PHRASE. The negative lookbehind excludes the common
# conversational "(what do) you think about X" — an opinion question that wants a live answer,
# not a deferred deep dive. A false positive still only costs one background think + an ack.
_THINK_PHRASE = re.compile(r"(?<!you )\bthink\s+(?:about|on)\b", re.IGNORECASE)

# Spoken immediately when a background reasoning job is kicked off (the live turn ends here;
# the real answer arrives later via brain.background_done — spec 11 §Purpose).
_THINKING_ACK = "On it, sir — I'll think that through and ping you when I have something."

# System prompt for the Sonnet summary of a finished opus job (spec 11 §on_done). The full
# opus detail is persisted to Memory; the owner hears this short spoken summary.
_SUMMARY_SYSTEM = (
    "You are EDITH. Summarize the following deep analysis for the owner in one or two spoken "
    "sentences, sir. Lead with the conclusion."
)
_SUMMARY_MAX_TOKENS = 120


class MemoryLike(Protocol):
    """The slice of the Memory contract Brain uses (north-star §4.3)."""

    def recall(self, query: str) -> list[dict[str, object]]: ...

    def remember(
        self, nodes: list[Node] | None = None, edges: list[Edge] | None = None
    ) -> None: ...


class RouterLike(Protocol):
    """The slice of the Router contract Brain uses (spec 05 §4.3)."""

    async def model_call(
        self,
        messages: list[dict[str, object]],
        tier_hint: Tier,
        max_tokens: int = ...,
    ) -> ModelResponse: ...


class BackgroundReasonerLike(Protocol):
    """The slice of the BackgroundReasoner contract Brain uses (spec 11).

    Injected so Brain stays decoupled from the reasoner's task machinery; the daemon wires the
    real ``BackgroundReasoner(router)``. Default None on Brain → no background, standalone.
    """

    async def think_async(
        self,
        messages: list[dict[str, object]],
        on_done: Callable[[ModelResponse], Awaitable[None]],
        *,
        max_tokens: int = ...,
    ) -> BackgroundJob: ...


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
        history: TurnBuffer | None = None,
        system_preamble: str | None = None,
        answer_max_tokens: int | None = None,
        reasoner: BackgroundReasonerLike | None = None,
    ) -> None:
        self._bus = bus
        self._memory = memory
        self._router = router
        # Voice persona + brevity (spec 10 §Persona/Brevity). Default None ->
        # the generic preamble and the Router's default max_tokens, so non-voice
        # callers (and every existing test) are unchanged. The daemon passes the
        # JARVIS "sir" persona + a tight cap for the spoken path.
        self._system_preamble = system_preamble or _SYSTEM_PREAMBLE
        self._answer_max_tokens = answer_max_tokens
        # Recent-turns buffer (spec 03 §Conversation memory — literal half).
        # Default None -> no splicing, no add: behaviour identical to the
        # pre-buffer loop, so every existing test stays green. When wired, prior
        # turns are spliced between the system message and the new utterance, and
        # the exchange trails the model call.
        self._history = history
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
        # Background reasoner (spec 11). Default None -> Brain never backgrounds: the explicit
        # "think about X" phrase falls through to the normal answer, and a deep-input turn just
        # answers live (no background pass) — standalone behavior, all existing tests green.
        self._reasoner = reasoner
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

        # 0b. EXPLICIT DEEP-WORK (spec 11): "think about X" → ack NOW, fire the deep opus
        # work in the background, and skip the normal live answer (the real answer arrives
        # later via brain.background_done). Only when a reasoner is wired; otherwise the
        # phrase falls straight through to the ordinary recall→answer path below.
        if self._reasoner is not None and _THINK_PHRASE.search(utterance):
            recalled = self._memory.recall(utterance)
            await self._start_background(self._build_messages(utterance, recalled), utterance)
            await self._publish_decision(_THINKING_ACK)
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

        # 2. ASSEMBLE + 3. REDACT + 3b. HISTORY (spec 03) — folded into _build_messages.
        safe_messages = self._build_messages(utterance, recalled)

        # 4. DECIDE (Sonnet — the live voice, latency-first). Catch the router's declared
        # transport errors so a network blip speaks a graceful apology instead of going silent —
        # the daemon has no other handler on this path. A failed exchange is NOT remembered/trailed.
        try:
            response = await self._answer(safe_messages)
        except MODEL_CALL_ERRORS:
            await self._publish_decision(_MODEL_ERROR_REPLY)
            return

        # 5. REMEMBER the exchange (never-persist filter runs again inside remember)
        self._remember_exchange(utterance, response.text)

        # 5b. TRAIL the exchange into the recent-turns buffer AFTER the model call
        # (so the buffer holds prior turns, never the in-flight one). Redact with
        # the same never-persist filter as _remember_exchange before storing.
        if self._history is not None:
            self._history.add("user", sanitize_text(utterance))
            self._history.add("assistant", sanitize_text(response.text))

        # 6. PUBLISH the decision
        await self._publish_decision(response.text)

        # 6b. PASSIVE BACKGROUND (spec 11): the live turn already answered on Sonnet; if the
        # OWNER'S INPUT itself is deep (a pasted log / long question — measured on the utterance
        # alone, NOT the accumulated context, so a long conversation never auto-fires opus on a
        # trivial turn), fire a background opus pass that reconsiders and pings a deeper answer.
        if self._reasoner is not None and _is_deep_input(utterance):
            await self._start_background(safe_messages, utterance)

    async def _publish_decision(self, answer: str) -> None:
        """Publish the plain-answer ``brain.decision`` (the daemon speaks it; spec 10)."""
        await self._bus.publish(
            "brain.decision",
            source="brain",
            payload={
                "intent": "answer_query",
                "action": "answer",
                "tier_used": _DEFAULT_TIER.value,
                "answer": answer,
            },
        )

    def _build_messages(
        self, utterance: str, recalled: list[dict[str, object]]
    ) -> list[dict[str, object]]:
        """Assemble → redact → splice history into the message list for a model call.

        Consolidates steps 2/3/3b so the live-answer path and the background path build the
        model context identically. History (spec 03) is spliced BETWEEN the system message and
        the current utterance; buffer content was already sanitized at add() time.
        """
        safe_messages = _redact(_assemble(utterance, recalled, self._system_preamble))
        if self._history is not None:
            history_messages: list[dict[str, object]] = [
                {"role": turn["role"], "content": turn["content"]}
                for turn in self._history.messages()
            ]
            safe_messages = [safe_messages[0], *history_messages, *safe_messages[1:]]
        return safe_messages

    async def _answer(self, safe_messages: list[dict[str, object]]) -> ModelResponse:
        """The live-answer model call (Sonnet — the low-latency voice)."""
        if self._answer_max_tokens is not None:
            return await self._router.model_call(
                safe_messages, _DEFAULT_TIER, self._answer_max_tokens
            )
        return await self._router.model_call(safe_messages, _DEFAULT_TIER)

    async def _start_background(
        self, messages: list[dict[str, object]], utterance: str
    ) -> None:
        """Fire a background opus job for ``messages``; on completion it summarizes + pings."""
        assert self._reasoner is not None  # guarded by every caller
        await self._reasoner.think_async(messages, self._make_on_done(utterance))

    def _make_on_done(self, utterance: str) -> Callable[[ModelResponse], Awaitable[None]]:
        """Build the completion callback: remember full detail → Sonnet-summarize → ping.

        Order matters: persist the (expensive) opus result FIRST and unconditionally, so a
        transport blip on the summary call can't discard the deep work. If the summary itself
        fails, the detail is safely in Memory and we simply skip the spoken ping this time.
        """

        async def on_done(result: ModelResponse) -> None:
            self._remember_background(utterance, result.text)
            try:
                summary = await self._summarize(result.text)
            except MODEL_CALL_ERRORS:
                return  # detail is persisted; a failed summary must not nuke it or crash the task
            await self._bus.publish(
                "brain.background_done",
                source="brain",
                payload={
                    "intent": "background_result",
                    "action": "answer",
                    "tier_used": Tier.OPUS.value,
                    "answer": summary,
                },
            )

        return on_done

    async def _summarize(self, detail: str) -> str:
        """One short Sonnet summary of the opus result — the spoken ping (spec 11)."""
        messages: list[dict[str, object]] = [
            {"role": "system", "content": _SUMMARY_SYSTEM},
            {"role": "user", "content": sanitize_text(detail)},
        ]
        response = await self._router.model_call(messages, Tier.SONNET, _SUMMARY_MAX_TOKENS)
        return response.text

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

    def _remember_background(self, utterance: str, detail: str) -> None:
        """Persist the FULL opus result (spec 11): the spoken ping is only the summary, but
        the deep detail is remembered so the next mention is an instant graph hit."""
        ts = str(time.time())
        text = sanitize_text(
            f"owner asked EDITH to think about: {utterance} | EDITH concluded: {detail}"
        )
        node = Node(label="Fact", id=f"think-{ts}", props={"text": text, "learned_at": ts})
        self._memory.remember(nodes=[node])


def _assemble(
    utterance: str,
    recalled: list[dict[str, object]],
    system_preamble: str = _SYSTEM_PREAMBLE,
) -> list[dict[str, object]]:
    """Working context = system preamble + recalled facts + the utterance."""
    facts = "\n".join(
        f"- {hit.get('text')}" for hit in recalled if hit.get("text")
    )
    system = system_preamble
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


def _is_deep_input(utterance: str) -> bool:
    """True when the owner's INPUT is large enough to be worth a background opus pass.

    Measured on the utterance ALONE (spec 11 §passive trigger) — a pasted log / stack trace /
    long question, not accumulated conversation. Keying off the whole assembled context instead
    would auto-fire opus on every turn once a session's history+recall grew past the threshold —
    an unthrottled cost leak while Guard's budget is deferred. Reuses the Router's own
    ``DEEP_TOKENS`` boundary so "deep" means the same thing here as in ``resolve_tier``.
    """
    message: list[dict[str, object]] = [{"role": "user", "content": utterance}]
    return estimate_tokens(message) > DEEP_TOKENS
