"""SessionQuerySkill — "what is session 2 doing?" (spec 04 §Step 4).

The owner-question path of session awareness. It is a Skill (not a second
``voice.utterance`` subscriber) so Brain's existing dispatch owns it and short-circuits
the answer loop — otherwise Brain AND the Narrator would both react to the same utterance.

Read-only observation of the owner's own machine → ``needs_confirmation = False`` (§6.3 AUTO).
It reads the live per-session snapshot from an injected ``states_provider`` (bound to
``SessionBus.session_states``), optionally phrases it via a haiku-tier model call (model-gated,
because it is an explicit owner request), and speaks the answer. The state payloads are already
redacted by SessionBus; ``speak`` (VoiceIO) redacts again on egress.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Protocol

from edith.router import ModelResponse, Tier
from edith.skills.base import SkillContext, SkillResult

StatesProvider = Callable[[], dict[str, dict[str, object]]]
Speak = Callable[[str], Awaitable[None]]

_SESSION_N = re.compile(r"session\s+#?(\d+)", re.IGNORECASE)

_PHRASE_PROMPT = (
    "You tell the owner what their coding sessions are doing, by voice, in one or two short "
    "spoken sentences. Below is the current redacted state of the relevant session(s). Be "
    "concrete and brief; do not invent detail beyond what is given."
)


async def _silent(_text: str) -> None:  # default speak seam (no VoiceIO wired)
    return None


class _RouterLike(Protocol):  # structural
    async def model_call(
        self, messages: list[dict[str, object]], tier_hint: Tier, max_tokens: int = ...
    ) -> ModelResponse: ...


class SessionQuerySkill:
    """Answer an owner's spoken question about running OMC / Claude sessions."""

    name = "session_query"
    needs_confirmation = False
    triggers = [
        "what is session",
        "what's session",
        "what are my sessions",
        "what are the sessions",
        "what's running",
        "what is running",
        "session status",
    ]

    def __init__(
        self,
        states_provider: StatesProvider,
        *,
        router: _RouterLike | None = None,
        speak: Speak = _silent,
    ) -> None:
        self._states = states_provider
        self._router = router
        self._speak = speak

    async def run(self, context: SkillContext) -> SkillResult:
        states = self._states()
        if not states:
            answer = "There are no active sessions right now."
            await self._speak(answer)
            return SkillResult(skill=self.name, findings=answer)

        target = self._select(context.utterance, states)
        facts = "\n".join(
            f"- {self._label(st)}: {st.get('state')}"
            f" ({st.get('current_action') or 'idle'})"
            for st in target
        )

        if self._router is not None:
            resp = await self._router.model_call(
                [
                    {"role": "system", "content": _PHRASE_PROMPT},
                    {"role": "user", "content": facts},
                ],
                Tier.HAIKU,
            )
            answer = resp.text
        else:
            answer = "Sessions — " + "; ".join(
                f"{self._label(st)} is {st.get('state')}" for st in target
            )

        await self._speak(answer)
        return SkillResult(skill=self.name, findings=answer)

    def _select(
        self, utterance: str, states: dict[str, dict[str, object]]
    ) -> list[dict[str, object]]:
        """Pick the session(s) the question is about: "session N", a repo name, or all."""
        ordered = [states[k] for k in sorted(states)]
        match = _SESSION_N.search(utterance)
        if match:
            idx = int(match.group(1)) - 1  # "session 2" → index 1 (1-based, owner-facing)
            if 0 <= idx < len(ordered):
                return [ordered[idx]]
        lowered = utterance.lower()
        by_repo = [st for st in ordered if str(st.get("repo") or "").lower() in lowered
                   and st.get("repo")]
        return by_repo or ordered

    @staticmethod
    def _label(state: dict[str, object]) -> str:
        repo = state.get("repo")
        if isinstance(repo, str) and repo:
            return repo
        sid = str(state.get("session_id", ""))
        return f"session {sid[:6]}" if sid else "a session"
