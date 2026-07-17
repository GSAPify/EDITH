"""Command parsing + repo resolution for desktop control (spec 06).

Pure logic, no OS side-effects — everything here is model-free and unit-testable.

``parse_command`` is the regex fast-path (spec 06 §Command parsing): it classifies the
top command shapes into a typed ``DesktopAction`` with zero model calls. A miss returns
``None`` so the caller may fall back to a haiku classify (the Skill does this only when a
Router is wired).

``RepoResolver`` maps a fuzzy repo name to an absolute path by scanning ``~/gitstuff/``
two levels deep (flat repos + repos cloned under org subdirs like ``patterninc/``) and
fuzzy-matching with ``difflib`` — no ML, no model call (spec 06 §Repo resolution). Genuine
ambiguity (the same basename in two locations) raises ``AmbiguousRepo`` so the Skill ASKs
rather than silently picking one.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class Intent(Enum):
    """The desktop actions v1 understands (spec 06 §Scope)."""

    OPEN_APP = "open_app"
    SPOTIFY = "spotify"
    TERMINAL = "terminal"  # open a visible Terminal.app window at a repo
    OMC_LAUNCH = "omc_launch"  # a terminal that also starts claude/OMC


@dataclass(frozen=True)
class DesktopAction:
    """A parsed, typed command. Optional fields are populated per intent."""

    intent: Intent
    app: str | None = None
    spotify_cmd: str | None = None  # "play" | "pause" | "next" | "volume"
    query: str | None = None  # track name for a play command
    volume: int | None = None  # 0-100 for a volume command
    repo: str | None = None  # fuzzy repo name for terminal / omc launch


# --- regex fast-path table (checked in order; first match wins) -------------
# Ordering matters: the OMC / terminal patterns are checked before the bare
# "open <app>" fallback so "open a terminal in X" is not read as "open <app>".

_OMC = re.compile(
    r"\b(?:start|launch|run|fire up)\s+(?:omc|oh[- ]?my[- ]?claude[- ]?code|claude)\b",
    re.IGNORECASE,
)
_TERMINAL_IN = re.compile(
    r"\bterminal\b.*?\b(?:in|at|inside)\s+(?:the\s+)?([\w.-]+)",
    re.IGNORECASE,
)
# Greedy capture — a lazy ``+?`` would stop at the first word boundary and truncate a
# hyphenated repo name ("brain-platform" -> "brain"). ``[\w.-]`` excludes spaces, so the
# greedy match stops cleanly at the next token; an optional trailing " repo" is dropped.
_OMC_IN = re.compile(
    r"\b(?:in|inside)\s+(?:the\s+)?([\w.-]+)(?:\s+repo\b)?",
    re.IGNORECASE,
)
_TERMINAL_BARE = re.compile(r"\bterminal\b", re.IGNORECASE)
_PLAY = re.compile(r"\bplay\s+(.+?)(?:\s+on\s+spotify)?\s*$", re.IGNORECASE)
_PAUSE = re.compile(r"\b(?:pause|stop)\b.*\b(?:music|spotify|song|track|playback)\b", re.IGNORECASE)
_NEXT = re.compile(r"\b(?:next\s+track|skip(?:\s+(?:this\s+)?(?:song|track))?)\b", re.IGNORECASE)
_VOLUME = re.compile(r"\b(?:set\s+)?(?:the\s+)?volume\s+to\s+(\d{1,3})\b", re.IGNORECASE)
_OPEN_APP = re.compile(r"\bopen\s+(?:(?:the|an?)\s+)?(.+?)\s*$", re.IGNORECASE)


def parse_command(utterance: str) -> DesktopAction | None:
    """Regex fast-path classify (spec 06 §Command parsing). ``None`` = no match."""
    text = utterance.strip()
    if not text:
        return None

    # Terminal / OMC first — they contain "in <repo>" which the open-app fallback
    # would otherwise swallow. "terminal ... and start OMC" => OMC launch.
    wants_omc = _OMC.search(text) is not None
    term_match = _TERMINAL_IN.search(text)
    if term_match:
        repo = term_match.group(1)
        return DesktopAction(
            intent=Intent.OMC_LAUNCH if wants_omc else Intent.TERMINAL, repo=repo
        )
    if wants_omc:
        in_match = _OMC_IN.search(text)
        if in_match:
            return DesktopAction(intent=Intent.OMC_LAUNCH, repo=in_match.group(1))

    # Bare "open a terminal" / "new terminal" (no "in <repo>") — a plain window, no cd.
    # Checked before the open-app fallback so it isn't read as ``open -a "a terminal"``.
    if _TERMINAL_BARE.search(text):
        return DesktopAction(intent=Intent.TERMINAL, repo=None)

    # Spotify transport (volume / next / pause before play — play is the greediest).
    vol = _VOLUME.search(text)
    if vol:
        # Clamp at parse so the parsed value, the AppleScript, and the spoken summary
        # all agree (the executor also clamps as a backstop).
        volume = min(100, max(0, int(vol.group(1))))
        return DesktopAction(intent=Intent.SPOTIFY, spotify_cmd="volume", volume=volume)
    if _NEXT.search(text):
        return DesktopAction(intent=Intent.SPOTIFY, spotify_cmd="next")
    if _PAUSE.search(text):
        return DesktopAction(intent=Intent.SPOTIFY, spotify_cmd="pause")
    play = _PLAY.search(text)
    if play:
        return DesktopAction(
            intent=Intent.SPOTIFY, spotify_cmd="play", query=play.group(1).strip()
        )

    # App launch fallback — anything left that starts with "open <name>".
    app = _OPEN_APP.search(text)
    if app:
        return DesktopAction(intent=Intent.OPEN_APP, app=_titlecase_app(app.group(1)))

    return None


def _titlecase_app(raw: str) -> str:
    """Normalize an app name from speech: "spotify" -> "Spotify"; keep multiword.

    ``open -a`` matches case-insensitively, so this is cosmetic (spoken summary) —
    but a clean name reads better. Single lowercase word -> capitalize; leave any
    name that already has capitals or spaces alone.
    """
    name = raw.strip()
    if name and name.islower() and " " not in name:
        return name.capitalize()
    return name


class AmbiguousRepo(Exception):
    """A fuzzy name matched more than one repo — the Skill must ASK, not guess."""

    def __init__(self, name: str, candidates: list[Path]) -> None:
        self.name = name
        self.candidates = candidates
        super().__init__(
            f"{name!r} is ambiguous: {', '.join(str(p) for p in candidates)}"
        )


class RepoNotFound(Exception):
    """No repo under the configured roots matched the fuzzy name."""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"no repo matched {name!r}")


class RepoResolver:
    """Fuzzy repo name -> absolute path (spec 06 §Repo resolution).

    Filesystem-first: the live graph's Repo nodes are metadata-only (empty ``path``),
    so the scan of ``~/gitstuff/`` is the primary mechanism; Memory is not consulted
    for paths in v1. Scans two levels: flat repos AND repos under org subdirs.
    """

    def __init__(self, roots: Sequence[Path] | None = None) -> None:
        self._roots = list(roots) if roots is not None else [Path.home() / "gitstuff"]
        self._index: dict[str, list[Path]] | None = None

    def resolve(self, fuzzy_name: str) -> Path:
        """Return the unique path for ``fuzzy_name``.

        Raises ``AmbiguousRepo`` when a name maps to >1 path, ``RepoNotFound`` on miss.
        """
        index = self._scan()
        name = fuzzy_name.strip().lower()

        # 1. exact (case-insensitive) basename match.
        if name in index:
            return self._one(fuzzy_name, index[name])

        # 2. substring containment (e.g. "concorde" in "concorde_lib").
        contained = [n for n in index if name in n or n in name]
        if len(contained) == 1:
            return self._one(fuzzy_name, index[contained[0]])
        if len(contained) > 1:
            # Prefer an exact-among-contained handled above; otherwise it's genuinely fuzzy.
            paths = [p for n in contained for p in index[n]]
            raise AmbiguousRepo(fuzzy_name, paths)

        # 3. difflib close match.
        close = difflib.get_close_matches(name, list(index), n=3, cutoff=0.6)
        if len(close) == 1:
            return self._one(fuzzy_name, index[close[0]])
        if len(close) > 1:
            paths = [p for n in close for p in index[n]]
            raise AmbiguousRepo(fuzzy_name, paths)

        raise RepoNotFound(fuzzy_name)

    @staticmethod
    def _one(name: str, paths: list[Path]) -> Path:
        if len(paths) == 1:
            return paths[0]
        # Same basename in >1 location. A flat ~/gitstuff/<name> and a nested
        # ~/gitstuff/<org>/<name> are the SAME repo cloned twice — the flat working
        # copy plus the bulk workspace pull (verified: identical remote). Prefer the
        # shallower (flat) copy. Genuine ambiguity is two copies at the SAME depth
        # (e.g. patterninc/<name> + ampmedia/<name>, no flat tiebreaker) -> ASK.
        shallowest = min(len(p.parts) for p in paths)
        top = [p for p in paths if len(p.parts) == shallowest]
        if len(top) == 1:
            return top[0]
        raise AmbiguousRepo(name, paths)

    def _scan(self) -> dict[str, list[Path]]:
        """Build (once) a basename -> [paths] index of git repos under the roots."""
        if self._index is not None:
            return self._index
        index: dict[str, list[Path]] = {}
        for root in self._roots:
            if not root.is_dir():
                continue
            for child in sorted(root.iterdir()):
                if not child.is_dir():
                    continue
                if (child / ".git").is_dir():
                    index.setdefault(child.name.lower(), []).append(child)
                    continue
                # An org dir (no .git of its own) — scan one level deeper.
                for grandchild in sorted(child.iterdir()):
                    if grandchild.is_dir() and (grandchild / ".git").is_dir():
                        index.setdefault(grandchild.name.lower(), []).append(grandchild)
        self._index = index
        return index
