"""DesktopControlSkill — voice-driven macOS automation (spec 06).

Turns an utterance ("open Spotify", "start OMC in concorde_lib") into a concrete OS
action: launch an app, drive Spotify, or open a Terminal.app window at a repo (optionally
starting claude/OMC in it). Parsing + resolution are model-free; a single haiku classify
fires only when the regex fast-path misses AND a Router is wired.

Every action in v1 is AUTO (spec 06 §Autonomy — open / play / cd / launch), so
``needs_confirmation`` is ``False``. Repo ambiguity is the one place the Skill
STOPS and asks (``SkillResult.asked``) rather than guessing.

**Autonomy gate (spec 11).** A ``guard`` may be injected; when it is, the parsed action's
verb (``Intent.value`` — ``open_app`` / ``spotify`` / ``terminal`` / ``omc_launch``) is put
to ``Guard.authorize`` BEFORE any ``Runner`` call, so a denylisted OS action is refused
instead of executed. **ASK is mapped to DENY, fail-closed**: there is no voice-confirm
implementation in the repo (``PRReviewSkill``'s ``Confirm`` seam defaults to deny and the
daemon wires that default), so an action that needs confirmation cannot be confirmed and
must not be assumed. Either way EDITH *says* why she refused rather than going quiet. When
no guard is injected the gate is absent and behaviour is exactly as before.

All OS access goes through the injected ``Runner`` seam and ``RepoResolver``, so the whole
flow is headless in tests. The real "Spotify opens / OMC starts" behaviour is owner
LIVE-SMOKE only.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Protocol

from edith.desktop.control import (
    AmbiguousRepo,
    DesktopAction,
    Intent,
    RepoNotFound,
    RepoResolver,
    parse_command,
)
from edith.desktop.executors import (
    Runner,
    default_runner,
    launch_app,
    open_terminal,
    spotify_command,
)
from edith.guard import Decision
from edith.router import ModelResponse, Tier
from edith.skills.base import SkillContext, SkillResult

Speak = Callable[[str], Awaitable[None]]

# Spoken refusals. DENY: the verb is on Guard's denylist. ASK: the action wants
# confirmation and nothing in the repo can ask for it yet, so it fails closed.
_DENIED_REPLY = "Sorry sir, I'm not permitted to do that."
_ASK_REPLY = (
    "That one needs your say-so, sir, and I have no way to ask you yet — so I haven't done it."
)


async def _silent(_text: str) -> None:
    """Default speak seam — no-op when no VoiceIO is wired."""


class _RouterLike(Protocol):  # structural — matches Brain's RouterLike
    async def model_call(
        self, messages: list[dict[str, object]], tier_hint: Tier, max_tokens: int = ...
    ) -> ModelResponse: ...


class _GuardLike(Protocol):  # structural — a real edith.guard.Guard satisfies it
    def authorize(self, action: str, *, needs_confirmation: bool = False) -> Decision: ...


_CLASSIFY_PROMPT = (
    "Classify the owner's desktop command into JSON. Respond with ONLY a JSON object, no "
    "prose. Keys: intent (one of open_app, spotify, terminal, omc_launch), and per intent: "
    "open_app -> app (string); spotify -> spotify_cmd (play|pause|next|volume), query "
    "(string, for play), volume (0-100, for volume); terminal|omc_launch -> repo (string). "
    "If it is not a desktop command, respond {\"intent\": \"none\"}."
)


class DesktopControlSkill:
    """Parse → (resolve repo) → execute via the seam → speak (spec 06)."""

    name = "desktop-control"
    # Broad desktop verbs. Registered LAST in edithd so pr-review / session_query
    # (more specific) win first; the parser is the real classifier behind these.
    triggers = [
        "open ",
        "play ",
        "pause",
        "skip",
        "next track",
        "spotify",
        "volume",
        "terminal",
        "start omc",
        "launch omc",
        "start claude",
        "run omc",
    ]
    needs_confirmation = False

    def __init__(
        self,
        *,
        runner: Runner = default_runner,
        resolver: RepoResolver | None = None,
        router: _RouterLike | None = None,
        speak: Speak = _silent,
        guard: _GuardLike | None = None,
    ) -> None:
        self._runner = runner
        self._resolver = resolver if resolver is not None else RepoResolver()
        self._router = router
        self._speak = speak
        # Autonomy gate (spec 11). None → no gate, unchanged behaviour; edithd injects
        # the daemon's single Guard.
        self._guard = guard

    async def run(self, context: SkillContext) -> SkillResult:
        action = parse_command(context.utterance)
        if action is None and self._router is not None:
            action = await self._classify_via_model(context.utterance)
        if action is None:
            # A broad trigger matched but this isn't a desktop command we can action.
            # DECLINE the turn (handled=False, no speak) so Brain falls through to the
            # recall→answer loop instead of dead-ending on "I didn't catch that".
            return SkillResult(skill=self.name, handled=False)

        # AUTONOMY GATE (spec 11) — before any Runner call, so a refused action has no
        # OS side effect. handled=True: refusing IS handling the turn; falling through
        # would have Brain answer an utterance we just declined to act on.
        decision = self._authorize(action)
        if decision is not Decision.ALLOW:
            return await self._speak_result(
                _DENIED_REPLY if decision is Decision.DENY else _ASK_REPLY
            )

        if action.intent is Intent.OPEN_APP:
            return await self._open_app(action)
        if action.intent is Intent.SPOTIFY:
            return await self._spotify(action)
        return await self._terminal(action)

    def _authorize(self, action: DesktopAction) -> Decision:
        """Put the action's verb to the Guard. No guard wired → ALLOW (unchanged)."""
        if self._guard is None:
            return Decision.ALLOW
        return self._guard.authorize(
            action.intent.value, needs_confirmation=self.needs_confirmation
        )

    async def _speak_result(self, summary: str) -> SkillResult:
        """Speak a summary (success or correction) and return a handled result."""
        await self._speak(summary)
        return SkillResult(skill=self.name, findings=summary)

    async def _open_app(self, action: DesktopAction) -> SkillResult:
        app = action.app or ""
        rc, _out = await launch_app(app, runner=self._runner)
        if rc != 0:
            return await self._speak_result(f"Sorry sir, I couldn't open {app}.")
        return await self._speak_result(f"Opening {app}.")

    async def _spotify(self, action: DesktopAction) -> SkillResult:
        rc, _out = await spotify_command(
            action.spotify_cmd or "",
            query=action.query,
            volume=action.volume,
            runner=self._runner,
        )
        if rc != 0:
            return await self._speak_result("Sorry sir, I couldn't reach Spotify.")
        summary = {
            "play": f"Playing {action.query}.",
            "pause": "Paused.",
            "next": "Skipping ahead.",
            "volume": f"Volume set to {action.volume}.",
        }.get(action.spotify_cmd or "", "Done.")
        return await self._speak_result(summary)

    async def _terminal(self, action: DesktopAction) -> SkillResult:
        # Bare "open a terminal" (no repo) -> a plain window at home, no resolve.
        if action.repo is None:
            path: Path = Path.home()
            target = "your home directory"
        else:
            try:
                path = self._resolver.resolve(action.repo)
            except AmbiguousRepo as exc:
                names = ", ".join(str(p) for p in exc.candidates)
                ask = (
                    f"I found more than one repo matching {action.repo!r}: {names}. "
                    "Which one, sir?"
                )
                await self._speak(ask)
                return SkillResult(skill=self.name, asked=ask)
            except RepoNotFound:
                ask = f"I couldn't find a repo called {action.repo!r} under your gitstuff, sir."
                await self._speak(ask)
                return SkillResult(skill=self.name, asked=ask)
            target = action.repo

        run_cmd = "claude" if action.intent is Intent.OMC_LAUNCH else None
        rc, _out = await open_terminal(path, run_cmd=run_cmd, runner=self._runner)
        if rc != 0:
            return await self._speak_result("Sorry sir, I couldn't open the terminal.")
        summary = (
            f"Starting OMC in {target}."
            if action.intent is Intent.OMC_LAUNCH
            else f"Terminal opened in {target}."
        )
        return await self._speak_result(summary)

    async def _classify_via_model(self, utterance: str) -> DesktopAction | None:
        """Haiku fallback (spec 06 §Command parsing step 2) — only when regex misses."""
        if self._router is None:
            return None
        resp = await self._router.model_call(
            [
                {"role": "system", "content": _CLASSIFY_PROMPT},
                {"role": "user", "content": utterance},
            ],
            Tier.HAIKU,
        )
        try:
            data = json.loads(resp.text)
        except (json.JSONDecodeError, TypeError):
            return None
        return _action_from_json(data)


def _action_from_json(data: object) -> DesktopAction | None:
    """Build a DesktopAction from the haiku classifier's JSON, or None if unusable."""
    if not isinstance(data, dict):
        return None
    raw_intent = data.get("intent")
    try:
        intent = Intent(raw_intent)
    except ValueError:
        return None  # "none" or anything unrecognized -> not a desktop command
    volume = data.get("volume")
    return DesktopAction(
        intent=intent,
        app=_as_str(data.get("app")),
        spotify_cmd=_as_str(data.get("spotify_cmd")),
        query=_as_str(data.get("query")),
        volume=int(volume) if isinstance(volume, (int, float)) else None,
        repo=_as_str(data.get("repo")),
    )


def _as_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
