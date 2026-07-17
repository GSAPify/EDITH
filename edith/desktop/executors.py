"""Thin OS-action functions for desktop control (spec 06 §Executor functions).

Each function constructs an argv and hands it to an injected ``Runner`` seam — the
default runs it via ``asyncio.create_subprocess_exec``; tests inject a recorder so no
subprocess ever fires. This is the only module that touches the OS, and it touches it
only through the seam, so the whole slice is headless-testable.

Terminal driver decision (deviation from spec 06 §Tech choices Option C): the spec
recommended a daemon-owned headless shell (``Popen``) for the OMC-launch path. But
``claude`` (the OMC entry-point) is an interactive TTY REPL and will not run under a
pipe-backed ``Popen``. So BOTH the "open a terminal" and the "start OMC" paths go
through Terminal.app ``do script`` — a real TTY, a visible window, and Slice 4's
SessionBus still narrates the session by tailing its transcript. One executor,
parameterized by ``run_cmd``; no ``Popen`` lifecycle in the daemon.
"""

from __future__ import annotations

import asyncio
import shlex
from collections.abc import Awaitable, Callable
from pathlib import Path

# argv -> (returncode, combined-output). Injected so tests never touch the OS.
Runner = Callable[[list[str]], Awaitable[tuple[int, str]]]


async def default_runner(argv: list[str]) -> tuple[int, str]:
    """Run ``argv`` with no shell; return (returncode, stdout+stderr)."""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    return proc.returncode or 0, out.decode(errors="replace")


async def launch_app(name: str, *, runner: Runner = default_runner) -> tuple[int, str]:
    """``open -a <name>`` — launch or focus a macOS app (case-insensitive match)."""
    return await runner(["open", "-a", name])


async def spotify_command(
    cmd: str,
    *,
    query: str | None = None,
    volume: int | None = None,
    runner: Runner = default_runner,
) -> tuple[int, str]:
    """Drive the Spotify desktop app via its AppleScript dictionary (no Web API creds)."""
    return await runner(["osascript", "-e", _spotify_script(cmd, query, volume)])


async def open_terminal(
    path: Path,
    *,
    run_cmd: str | None = None,
    runner: Runner = default_runner,
) -> tuple[int, str]:
    """Open a Terminal.app window, ``cd`` to ``path``, optionally run ``run_cmd`` (e.g. claude).

    SECURITY INVARIANT: ``run_cmd`` is interpolated into the shell command UNESCAPED and
    must only ever be a hardcoded literal (today: the constant ``"claude"`` at the one call
    site). It is NOT a command-injection sink as written, but the moment a parsed or
    model-derived value is allowed to flow here it becomes one — quote/allowlist it then.
    ``path`` is always a real filesystem path from RepoResolver and is ``shlex.quote``d.
    """
    inner = f"cd {shlex.quote(str(path))}"
    if run_cmd:
        inner += f" && {run_cmd}"
    script = f'tell application "Terminal" to do script {_applescript_str(inner)}'
    return await runner(["osascript", "-e", script])


def _spotify_script(cmd: str, query: str | None, volume: int | None) -> str:
    """Build the AppleScript for a Spotify transport command."""
    prefix = 'tell application "Spotify" to '
    if cmd == "play" and query:
        # Spotify resolves spotify:search:<q> to its top match. Route the whole URI
        # through the SAME escaper the terminal path uses (backslash-then-quote) so both
        # OS-touching paths escape identically — a query ending in a backslash or quote
        # can't malform the AppleScript literal.
        return f"{prefix}play track {_applescript_str('spotify:search:' + query)}"
    if cmd == "pause":
        return f"{prefix}pause"
    if cmd == "next":
        return f"{prefix}next track"
    if cmd == "volume" and volume is not None:
        clamped = max(0, min(100, volume))
        return f"{prefix}set sound volume to {clamped}"
    raise ValueError(f"unsupported spotify command: {cmd!r}")


def _applescript_str(value: str) -> str:
    """Wrap ``value`` as an AppleScript string literal (escape backslash then quote)."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
