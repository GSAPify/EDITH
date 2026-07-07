"""Control API server + client over a REAL unix domain socket (north-star §4.2).

No mocks for the socket layer: each test starts ``ControlServer`` on a tmp
socket and drives it with the real ``ControlClient``. JSON-lines request/response.

Asserted behaviors (task TDD list):
  - pause -> status shows "paused"; resume -> "running",
  - unknown cmd -> structured error {"ok": false, ...},
  - status returns the LOCKED shape exactly: {state, active_skill, budget_used,
    last_event} — exact key set, not just presence,
  - the socket file perms are 0600,
  - kill flips state to "stopping",
  - budget_used comes from the injected BudgetView (0 until Guard lands).
"""

from __future__ import annotations

import stat
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest

from edith.daemon.client import ControlClient
from edith.daemon.control import BudgetView, ControlServer
from edith.daemon.state import RuntimeState


@pytest.fixture
def sock_path() -> Iterator[Path]:
    """A short-path unix socket.

    pytest's ``tmp_path`` is nested deep enough to exceed macOS's ~104-byte
    ``sun_path`` limit, so a socket created there fails to bind. Production uses
    a short path ($EDITH_DATA_DIR/edithd.sock), so we mirror that here with a
    short system-temp dir rather than distorting the server for the test env.
    """
    with tempfile.TemporaryDirectory(dir="/tmp") as d:  # noqa: S108 - short path needed for sun_path limit
        yield Path(d) / "edithd.sock"


class ZeroBudget:
    """The seam for Guard's budget. Returns 0 until the Guard slice lands."""

    def budget_used(self) -> int:
        return 0


async def _server_on(sock: Path, state: RuntimeState) -> tuple[ControlServer, Path]:
    server = ControlServer(
        socket_path=sock,
        state=state,
        budget=ZeroBudget(),
        on_kill=lambda: None,
    )
    await server.start()
    return server, sock


async def test_status_returns_locked_shape(sock_path):
    state = RuntimeState()
    state.active_skill = "pr-review"
    state.last_event = "brain.decision"
    server, sock = await _server_on(sock_path, state)
    try:
        resp = await ControlClient(sock).send({"cmd": "status"})
    finally:
        await server.stop()

    assert resp["ok"] is True
    status = cast(dict[str, object], resp["status"])
    # LOCKED shape — exact key set, nothing more, nothing less.
    assert set(status) == {"state", "active_skill", "budget_used", "last_event"}
    assert status["state"] == "running"
    assert status["active_skill"] == "pr-review"
    assert status["last_event"] == "brain.decision"
    assert status["budget_used"] == 0


async def test_pause_then_status_shows_paused(sock_path):
    state = RuntimeState()
    server, sock = await _server_on(sock_path, state)
    try:
        client = ControlClient(sock)
        pause_resp = await client.send({"cmd": "pause"})
        status = cast(dict[str, object], (await client.send({"cmd": "status"}))["status"])
    finally:
        await server.stop()

    assert pause_resp["ok"] is True
    assert status["state"] == "paused"


async def test_resume_returns_to_running(sock_path):
    state = RuntimeState()
    server, sock = await _server_on(sock_path, state)
    try:
        client = ControlClient(sock)
        await client.send({"cmd": "pause"})
        await client.send({"cmd": "resume"})
        status = cast(dict[str, object], (await client.send({"cmd": "status"}))["status"])
    finally:
        await server.stop()

    assert status["state"] == "running"


async def test_kill_transitions_to_stopping_and_fires_callback(sock_path):
    state = RuntimeState()
    killed: list[bool] = []
    sock = sock_path
    server = ControlServer(
        socket_path=sock,
        state=state,
        budget=ZeroBudget(),
        on_kill=lambda: killed.append(True),
    )
    await server.start()
    try:
        resp = await ControlClient(sock).send({"cmd": "kill"})
    finally:
        await server.stop()

    assert resp["ok"] is True
    assert state.state.value == "stopping"
    assert killed == [True]


async def test_unknown_cmd_is_structured_error(sock_path):
    state = RuntimeState()
    server, sock = await _server_on(sock_path, state)
    try:
        resp = await ControlClient(sock).send({"cmd": "explode"})
    finally:
        await server.stop()

    assert resp["ok"] is False
    assert "error" in resp


async def test_missing_cmd_is_structured_error(sock_path):
    state = RuntimeState()
    server, sock = await _server_on(sock_path, state)
    try:
        resp = await ControlClient(sock).send({"not_a_cmd": "x"})
    finally:
        await server.stop()

    assert resp["ok"] is False
    assert "error" in resp


async def test_socket_perms_are_0600(sock_path):
    state = RuntimeState()
    server, sock = await _server_on(sock_path, state)
    try:
        mode = stat.S_IMODE(sock.stat().st_mode)
    finally:
        await server.stop()

    assert mode == 0o600


async def test_stop_removes_the_socket_file(sock_path):
    state = RuntimeState()
    server, sock = await _server_on(sock_path, state)
    await server.stop()
    assert not sock.exists()


async def test_concurrent_clients_share_one_state(sock_path):
    # Two independent client connections observe the same daemon state.
    state = RuntimeState()
    server, sock = await _server_on(sock_path, state)
    try:
        await ControlClient(sock).send({"cmd": "pause"})
        # a fresh connection sees the pause the first connection caused
        reply = await ControlClient(sock).send({"cmd": "status"})
        status = cast(dict[str, object], reply["status"])
    finally:
        await server.stop()

    assert status["state"] == "paused"


async def test_budget_view_is_the_seam(sock_path):
    # A non-zero BudgetView flows straight through status (proves the seam wiring).
    class StubBudget:
        def budget_used(self) -> int:
            return 4200

    state = RuntimeState()
    sock = sock_path
    server = ControlServer(
        socket_path=sock, state=state, budget=StubBudget(), on_kill=lambda: None
    )
    await server.start()
    try:
        reply = await ControlClient(sock).send({"cmd": "status"})
        status = cast(dict[str, object], reply["status"])
    finally:
        await server.stop()

    assert status["budget_used"] == 4200


def test_budgetview_protocol_is_runtime_checkable():
    # The seam is a typing.Protocol; ZeroBudget satisfies it structurally.
    assert isinstance(ZeroBudget(), BudgetView)


async def test_client_helper_raises_on_missing_socket(sock_path):
    # Sending to a socket that was never created is an OSError, not a hang.
    client = ControlClient(sock_path)
    with pytest.raises(OSError):
        await client.send({"cmd": "status"})
