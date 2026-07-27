"""Tests for the menu-bar control logic (item 3, EDITH operationalization roadmap).

Headless: no real unix socket, no ``rumps``, no real ``edithd``. Every Control API
round-trip goes through ``ScriptedClient``, a fake that satisfies
``ControlClientLike`` structurally and returns canned responses (or raises) in
order — the same "inject the seam" pattern ``test_desktop_control.py`` uses for
its ``_RecordingRunner``.

Covers the task's four required surfaces:
  1. label formatting for each daemon state (+ active_skill / unknown state),
  2. command dispatch (pause/resume/kill send the right ``cmd`` and return the reply),
  3. the daemon-not-running path (status poll AND a command both degrade calmly),
  4. a socket that dies mid-session (works, then the next call raises OSError).

The real ``rumps`` presentation layer and the menu bar actually appearing are
owner LIVE-SMOKE only — not exercised here.
"""

from __future__ import annotations

import pytest

from edith.menubar.controller import (
    NOT_RUNNING_LABEL,
    MenuBarController,
    format_label,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class ScriptedClient:
    """Fake Control API client — pops one canned response/exception per ``send()``."""

    def __init__(self, script: list[dict[str, object] | Exception]) -> None:
        self._script = list(script)
        self.requests: list[dict[str, object]] = []

    async def send(self, request: dict[str, object]) -> dict[str, object]:
        self.requests.append(request)
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _status_response(
    state: str, active_skill: str | None = None, budget_used: int = 0, last_event: str | None = None
) -> dict[str, object]:
    return {
        "ok": True,
        "status": {
            "state": state,
            "active_skill": active_skill,
            "budget_used": budget_used,
            "last_event": last_event,
        },
    }


# ---------------------------------------------------------------------------
# 1. Label formatting
# ---------------------------------------------------------------------------


def test_format_label_running_no_active_skill():
    status = {"state": "running", "active_skill": None, "budget_used": 0, "last_event": None}
    assert format_label(status) == "EDITH — running"


def test_format_label_paused():
    status = {"state": "paused", "active_skill": None, "budget_used": 0, "last_event": None}
    assert format_label(status) == "EDITH — paused"


def test_format_label_stopping():
    status = {"state": "stopping", "active_skill": None, "budget_used": 0, "last_event": None}
    assert format_label(status) == "EDITH — stopping"


def test_format_label_includes_active_skill():
    status = {
        "state": "running",
        "active_skill": "pr-review",
        "budget_used": 12,
        "last_event": "skill.result",
    }
    assert format_label(status) == "EDITH — running (pr-review)"


def test_format_label_unknown_state_shown_verbatim():
    # A future daemon state shouldn't silently vanish from the bar.
    status = {"state": "resurrecting", "active_skill": None, "budget_used": 0, "last_event": None}
    assert format_label(status) == "EDITH — resurrecting"


# ---------------------------------------------------------------------------
# 2. Status polling (refresh)
# ---------------------------------------------------------------------------


async def test_refresh_formats_the_live_status():
    client = ScriptedClient([_status_response("running", active_skill="pr-review")])
    controller = MenuBarController(client)

    label = await controller.refresh()

    assert label == "EDITH — running (pr-review)"
    assert client.requests == [{"cmd": "status"}]
    assert controller.last_status == {
        "state": "running",
        "active_skill": "pr-review",
        "budget_used": 0,
        "last_event": None,
    }


async def test_refresh_reflects_paused_then_running_across_polls():
    client = ScriptedClient([_status_response("paused"), _status_response("running")])
    controller = MenuBarController(client)

    first = await controller.refresh()
    second = await controller.refresh()

    assert first == "EDITH — paused"
    assert second == "EDITH — running"


# ---------------------------------------------------------------------------
# 3. Daemon not running
# ---------------------------------------------------------------------------


async def test_refresh_when_daemon_not_running_is_not_running_label():
    # No socket file -> asyncio.open_unix_connection raises FileNotFoundError (an OSError).
    client = ScriptedClient([FileNotFoundError("no such socket")])
    controller = MenuBarController(client)

    label = await controller.refresh()

    assert label == NOT_RUNNING_LABEL
    assert controller.last_status is None


async def test_refresh_when_response_not_ok_is_not_running_label():
    client = ScriptedClient([{"ok": False, "error": "boom"}])
    controller = MenuBarController(client)

    label = await controller.refresh()

    assert label == NOT_RUNNING_LABEL


async def test_refresh_when_status_field_malformed_is_not_running_label():
    client = ScriptedClient([{"ok": True, "status": "not-a-dict"}])
    controller = MenuBarController(client)

    label = await controller.refresh()

    assert label == NOT_RUNNING_LABEL


async def test_pause_when_daemon_not_running_returns_structured_error():
    client = ScriptedClient([ConnectionRefusedError("nobody listening")])
    controller = MenuBarController(client)

    result = await controller.pause()

    assert result["ok"] is False
    assert "daemon not reachable" in str(result["error"])


# ---------------------------------------------------------------------------
# 4. Command dispatch
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "cmd"),
    [("pause", "pause"), ("resume", "resume"), ("kill", "kill")],
)
async def test_command_dispatch_sends_the_right_cmd(method, cmd):
    client = ScriptedClient([{"ok": True}])
    controller = MenuBarController(client)

    result = await getattr(controller, method)()

    assert client.requests == [{"cmd": cmd}]
    assert result == {"ok": True}


async def test_kill_transition_error_passes_through():
    # e.g. kill while already STOPPING -> the daemon's structured error, not a crash.
    client = ScriptedClient([{"ok": False, "error": "cannot kill while STOPPING"}])
    controller = MenuBarController(client)

    result = await controller.kill()

    assert result == {"ok": False, "error": "cannot kill while STOPPING"}


# ---------------------------------------------------------------------------
# Socket goes away mid-session
# ---------------------------------------------------------------------------


async def test_socket_dies_mid_session_between_two_polls():
    # First poll: daemon is up. Second poll: the socket has vanished (owner killed
    # edithd, or it crashed) — refresh must degrade, not raise, on the very next call.
    client = ScriptedClient(
        [_status_response("running"), FileNotFoundError("socket gone")]
    )
    controller = MenuBarController(client)

    first = await controller.refresh()
    second = await controller.refresh()

    assert first == "EDITH — running"
    assert second == NOT_RUNNING_LABEL
    assert controller.last_status is None


async def test_socket_dies_between_a_successful_poll_and_a_pause_command():
    client = ScriptedClient(
        [_status_response("running"), BrokenPipeError("connection reset")]
    )
    controller = MenuBarController(client)

    await controller.refresh()
    result = await controller.pause()

    assert result["ok"] is False
    assert "daemon not reachable" in str(result["error"])
