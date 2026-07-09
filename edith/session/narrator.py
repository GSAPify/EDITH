"""Narrator — the three-class narration policy (spec 04 §Step 3, §Cost/token notes).

Subscribes to ``session.event`` / ``session.state`` and decides, per event, whether EDITH
says anything and whether that costs a model call. This is the always-on token-burner the
north-star warns about, so the policy gates hard:

  • **Silent** (default): routine ``tool_use`` / ``prompt`` / working-state refreshes →
    nothing spoken, no model call.
  • **Spoken locally**: ``start`` / ``stop`` / ``error`` and the idle→waiting transition →
    ``speak`` a locally-composed template string. No model call.
  • **Model-gated** (rarest): an ``error`` when a Router is wired and the budget gate allows
    → one terse haiku-tier spoken summary of the (already-redacted) error. This is the v1
    slice of the killer demo ("Session 2 hit an error — it's retrying the DB query").

Design note (deviation from spec wording): the spec says "narration policy in Brain". It lives
in this dedicated collaborator instead — it consumes different bus topics with its own gating
and is independently testable, which keeps Brain's utterance loop single-purpose. edithd wires
a Narrator alongside Brain. Owner questions ("what is session 2 doing?") are a Skill, not a
Narrator path — otherwise Brain and the Narrator would both answer the same ``voice.utterance``.

Idle is a **timer**, not a record: the daemon calls ``tick()`` periodically; a session with no
activity for ``idle_seconds`` is announced as waiting, once, until it acts again.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Protocol

from edith.bus import Event, EventBus
from edith.router import ModelResponse, Tier

Speak = Callable[[str], Awaitable[None]]

_NARRATION_PROMPT = (
    "You narrate a coding session for its owner by voice. Given the one-line, "
    "already-redacted event below, reply with ONE short spoken sentence (no more than "
    "20 words) telling the owner what happened and what the session is doing about it."
)


class _RouterLike(Protocol):  # structural; a real Router satisfies it
    async def model_call(
        self, messages: list[dict[str, object]], tier_hint: Tier, max_tokens: int = ...
    ) -> ModelResponse: ...


class Narrator:
    """Applies the three-class narration policy to session bus events."""

    def __init__(
        self,
        bus: EventBus,
        speak: Speak,
        *,
        router: _RouterLike | None = None,
        budget_gate: Callable[[], bool] = lambda: True,
        clock: Callable[[], float] = time.time,
        idle_seconds: float = 120.0,
        error_cooldown: float = 90.0,
    ) -> None:
        self._speak = speak
        self._router = router
        # Budget seam: Guard (a later slice) owns the real per-window budget. Until
        # then this defaults to always-allow; edithd can inject a real gate.
        self._budget_gate = budget_gate
        self._clock = clock
        self._idle_seconds = idle_seconds
        # Per-session error-narration throttle (spec §Cost: "O(1) model calls per
        # notable event, not O(N) per tool call"). Every failing tool_result is an
        # error event, so a real session emits many; collapse a burst to one
        # narration until the cooldown elapses. This is an app-level rate limit,
        # NOT Guard's per-window token budget (that cross-cutting slice is deferred).
        self._error_cooldown = error_cooldown
        self._last_error_narration: dict[str, float] = {}
        self._last_activity: dict[str, float] = {}
        self._repo: dict[str, str | None] = {}
        self._narrated_idle: set[str] = set()
        bus.subscribe("session.event", self._on_event)
        bus.subscribe("session.state", self._on_state)

    async def _on_state(self, event: Event) -> None:
        sid = str(event.payload.get("session_id", ""))
        if not sid:
            return
        self._repo[sid] = event.payload.get("repo")  # type: ignore[assignment]
        self._last_activity.setdefault(sid, self._clock())

    async def _on_event(self, event: Event) -> None:
        p = event.payload
        sid = str(p.get("session_id", ""))
        if not sid:
            return
        kind = p.get("kind")
        # Any real event is activity → reset the idle timer + clear the idle latch.
        self._last_activity[sid] = self._clock()
        self._narrated_idle.discard(sid)
        self._repo.setdefault(sid, p.get("repo"))  # type: ignore[arg-type]

        if kind in ("tool_use", "prompt"):
            return  # SILENT class
        if kind == "start":
            await self._speak(f"Session started in {self._label(sid, p.get('repo'))}.")
        elif kind == "stop":
            await self._speak(f"Session in {self._label(sid, p.get('repo'))} finished.")
        elif kind == "error":
            await self._narrate_error(sid, p)

    async def _narrate_error(self, sid: str, payload: dict[str, object]) -> None:
        # Throttle: at most one error narration per session per cooldown window, so a
        # cascade of failing tool_results (each an error event) does not become a
        # cascade of model calls / spoken lines.
        now = self._clock()
        last = self._last_error_narration.get(sid)
        if last is not None and now - last < self._error_cooldown:
            return
        self._last_error_narration[sid] = now

        summary = str(payload.get("summary", "an error"))
        if self._router is not None and self._budget_gate():
            # MODEL-GATED: one terse spoken line from the redacted summary.
            resp = await self._router.model_call(
                [
                    {"role": "system", "content": _NARRATION_PROMPT},
                    {"role": "user", "content": summary},
                ],
                Tier.HAIKU,
            )
            await self._speak(resp.text)
        else:
            # SPOKEN-LOCAL fallback: template, no model.
            await self._speak(f"Something errored in {self._label(sid, payload.get('repo'))}.")

    async def tick(self) -> None:
        """Idle sweep (timer-driven): announce, once, any session gone quiet."""
        now = self._clock()
        for sid, last in list(self._last_activity.items()):
            if sid in self._narrated_idle:
                continue
            if now - last >= self._idle_seconds:
                self._narrated_idle.add(sid)
                await self._speak(f"Session in {self._label(sid)} is now waiting.")

    def _label(self, sid: str, repo: object = None) -> str:
        name = repo if isinstance(repo, str) and repo else self._repo.get(sid)
        return name if isinstance(name, str) and name else f"session {sid[:6]}"
