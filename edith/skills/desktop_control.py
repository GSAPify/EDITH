"""DesktopControlSkill — voice-driven macOS automation (spec 06).

Turns an utterance ("open Spotify", "start OMC in concorde_lib") into a concrete OS
action: launch an app, drive Spotify, or open a Terminal.app window at a repo (optionally
starting claude/OMC in it). Parsing + resolution are model-free; a single haiku classify
fires only when the regex fast-path misses AND a Router is wired.

Every action in v1 is AUTO (spec 06 §Autonomy — open / play / cd / launch), so
``needs_confirmation`` is ``False`` and there is no ASK/DENY branch: the parser simply
never emits an action outside the AUTO set. Repo ambiguity is the one place the Skill
STOPS and asks (``SkillResult.asked``) rather than guessing.

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
from edith.router import ModelResponse, Tier
from edith.skills.base import SkillContext, SkillResult

Speak = Callable[[str], Awaitable[None]]


async def _silent(_text: str) -> None:
    """Default speak seam — no-op when no VoiceIO is wired."""


class _RouterLike(Protocol):  # structural — matches Brain's RouterLike
    async def model_call(
        self, messages: list[dict[str, object]], tier_hint: Tier, max_tokens: int = ...
    ) -> ModelResponse: ...


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
    ) -> None:
        self._runner = runner
        self._resolver = resolver if resolver is not None else RepoResolver()
        self._router = router
        self._speak = speak

    async def run(self, context: SkillContext) -> SkillResult:
        action = parse_command(context.utterance)
        if action is None and self._router is not None:
            action = await self._classify_via_model(context.utterance)
        if action is None:
            msg = "Sorry sir, I didn't catch that command."
            await self._speak(msg)
            return SkillResult(skill=self.name, findings=msg)

        if action.intent is Intent.OPEN_APP:
            return await self._open_app(action)
        if action.intent is Intent.SPOTIFY:
            return await self._spotify(action)
        return await self._terminal(action)

    async def _open_app(self, action: DesktopAction) -> SkillResult:
        app = action.app or ""
        await launch_app(app, runner=self._runner)
        summary = f"Opening {app}."
        await self._speak(summary)
        return SkillResult(skill=self.name, findings=summary)

    async def _spotify(self, action: DesktopAction) -> SkillResult:
        await spotify_command(
            action.spotify_cmd or "",
            query=action.query,
            volume=action.volume,
            runner=self._runner,
        )
        summary = {
            "play": f"Playing {action.query}.",
            "pause": "Paused.",
            "next": "Skipping ahead.",
            "volume": f"Volume set to {action.volume}.",
        }.get(action.spotify_cmd or "", "Done.")
        await self._speak(summary)
        return SkillResult(skill=self.name, findings=summary)

    async def _terminal(self, action: DesktopAction) -> SkillResult:
        try:
            path: Path = self._resolver.resolve(action.repo or "")
        except AmbiguousRepo as exc:
            names = ", ".join(str(p) for p in exc.candidates)
            ask = f"I found more than one repo matching {action.repo!r}: {names}. Which one, sir?"
            await self._speak(ask)
            return SkillResult(skill=self.name, asked=ask)
        except RepoNotFound:
            ask = f"I couldn't find a repo called {action.repo!r} under your gitstuff, sir."
            await self._speak(ask)
            return SkillResult(skill=self.name, asked=ask)

        run_cmd = "claude" if action.intent is Intent.OMC_LAUNCH else None
        await open_terminal(path, run_cmd=run_cmd, runner=self._runner)
        summary = (
            f"Starting OMC in {action.repo}."
            if action.intent is Intent.OMC_LAUNCH
            else f"Terminal opened in {action.repo}."
        )
        await self._speak(summary)
        return SkillResult(skill=self.name, findings=summary)

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
