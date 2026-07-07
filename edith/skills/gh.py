"""Injectable async ``gh`` runner (spec 02 §Dependencies, §Tech choices).

All GitHub I/O for the PR-review skill goes through ``run_gh``. It shells out via
``asyncio.create_subprocess_exec`` with an ARG LIST — never a shell string — so a
name or title carried in from an utterance can never be interpreted as shell. The
``gh`` CLI manages its own token via the macOS Keychain; EDITH never touches it.

``GhRunner`` is a type alias so the skill takes the runner as an injectable dep;
tests substitute a fake and never hit GitHub.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

# name -> stdout. Injected into PRReviewSkill so tests never touch GitHub.
GhRunner = Callable[[list[str]], Awaitable[str]]


class GhError(RuntimeError):
    """A ``gh`` invocation exited nonzero; carries the stderr text."""


async def run_gh(args: list[str]) -> str:
    """Run ``gh <args...>`` and return decoded stdout.

    Raises ``GhError`` with the stderr on a nonzero exit. ``FileNotFoundError``
    (gh not installed) is surfaced as ``GhError`` too so callers handle one type.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "gh",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise GhError("gh CLI not found on PATH") from exc

    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise GhError(stderr.decode(errors="replace").strip())
    return stdout.decode(errors="replace")
