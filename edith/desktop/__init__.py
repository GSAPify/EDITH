"""Desktop control (spec 06): voice-driven macOS automation.

Parsing + repo resolution (``control``) are pure logic; the executors (``executors``)
are the only OS-touching code and go through an injectable ``Runner`` seam. The
``DesktopControlSkill`` (in ``edith.skills.desktop_control``) wires them into Brain.
"""

from __future__ import annotations

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

__all__ = [
    "AmbiguousRepo",
    "DesktopAction",
    "Intent",
    "RepoNotFound",
    "RepoResolver",
    "Runner",
    "default_runner",
    "launch_app",
    "open_terminal",
    "parse_command",
    "spotify_command",
]
