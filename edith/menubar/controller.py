"""Menu-bar control logic — headless-testable, no GUI import (north-star §4.2).

Everything a menu-bar app needs beyond drawing pixels lives here: polling
``status``, formatting the title label, mapping a menu click to a Control API
command, and degrading gracefully when ``edithd`` isn't running or the socket
disappears mid-session. ``edith/menubar/app.py`` is a thin ``rumps`` shell that
calls straight into this module — mirrors how ``DesktopControlSkill`` takes an
injected ``Runner``/``RepoResolver`` seam instead of touching the OS directly.

The daemon not running is the COMMON case today (nothing keeps ``edithd`` up
yet — see the roadmap's operationalization items), not an edge case, so every
public method here degrades to a calm status/result rather than raising.
"""

from __future__ import annotations

from typing import Protocol

# LOCKED status shape (north-star §4.2, edith/daemon/control.py:_status): exactly
# these four keys. Only state + active_skill feed the title; budget_used and
# last_event are available on `last_status` for a future menu body / tooltip.
_STATE_LABELS = {
    "running": "running",
    "paused": "paused",
    "stopping": "stopping",
}

NOT_RUNNING_LABEL = "EDITH — not running"


class ControlClientLike(Protocol):
    """The slice of ``ControlClient`` the controller needs (structural seam).

    Mirrors ``_RouterLike`` in ``edith/skills/desktop_control.py``: tests hand in a
    scripted fake instead of a real ``edith.daemon.client.ControlClient``, so no
    socket is ever opened in the unit test suite.
    """

    async def send(self, request: dict[str, object]) -> dict[str, object]: ...


def format_label(status: dict[str, object]) -> str:
    """Format a ``status`` response payload into the menu-bar title.

    ``state`` drives the label; an ``active_skill`` (set while a skill is
    mid-run) is appended for visibility. An unrecognized state string is shown
    verbatim rather than swallowed, so a future daemon state doesn't silently
    disappear from the bar.
    """
    state = str(status.get("state", "unknown"))
    label = _STATE_LABELS.get(state, state)
    active_skill = status.get("active_skill")
    if active_skill:
        return f"EDITH — {label} ({active_skill})"
    return f"EDITH — {label}"


class MenuBarController:
    """Polls status and dispatches pause/resume/kill over an injected Control API client."""

    def __init__(self, client: ControlClientLike) -> None:
        self._client = client
        # The last successfully-parsed status payload (all four LOCKED keys), or
        # None while the daemon is unreachable / the response was malformed.
        self.last_status: dict[str, object] | None = None

    async def refresh(self) -> str:
        """Poll ``status`` and return the formatted title. Never raises.

        A daemon that isn't running (no socket file) or one whose socket dies
        mid-session both surface as ``OSError`` from the underlying client —
        both collapse to :data:`NOT_RUNNING_LABEL` rather than crashing the
        polling timer.
        """
        try:
            response = await self._client.send({"cmd": "status"})
        except OSError:
            self.last_status = None
            return NOT_RUNNING_LABEL
        status = response.get("status") if response.get("ok") else None
        if not isinstance(status, dict):
            self.last_status = None
            return NOT_RUNNING_LABEL
        self.last_status = status
        return format_label(status)

    async def pause(self) -> dict[str, object]:
        """Send ``pause``. See :meth:`_send_command` for the not-reachable path."""
        return await self._send_command("pause")

    async def resume(self) -> dict[str, object]:
        """Send ``resume``. See :meth:`_send_command` for the not-reachable path."""
        return await self._send_command("resume")

    async def kill(self) -> dict[str, object]:
        """Send ``kill``. Destructive — ``app.py`` confirms before calling this.

        See :meth:`_send_command` for the not-reachable path.
        """
        return await self._send_command("kill")

    async def _send_command(self, cmd: str) -> dict[str, object]:
        """Dispatch one Control API command, turning a dead socket into a structured error."""
        try:
            return await self._client.send({"cmd": cmd})
        except OSError as exc:
            return {"ok": False, "error": f"daemon not reachable: {exc}"}
