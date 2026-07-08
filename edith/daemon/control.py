"""Control API server — asyncio unix-domain-socket, JSON-lines (north-star §4.2).

The menu-bar app (and tests) send one JSON object per line — ``{"cmd": "..."}`` —
and read one JSON object back: ``{"ok": true, ...}`` on success or
``{"ok": false, "error": "..."}`` on failure. The four locked commands are
``pause`` / ``resume`` / ``kill`` / ``status``; ``status`` returns the LOCKED
shape ``{state, active_skill, budget_used, last_event}``.

This is socket-only by construction — ``asyncio.start_unix_server`` binds a
filesystem path, never a TCP port (north-star §4.2: "socket only, NEVER a public
network bind"). The socket file is created 0600 (owner-only) so no other local
user can drive the daemon. On ``stop`` the socket file is removed.

``budget_used`` comes from an injected ``BudgetView``. The Guard component that
owns the real budget is a later cross-cutting slice; the dev seam returns 0.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, runtime_checkable

from edith.daemon.state import RuntimeState

_SOCKET_MODE = 0o600


@runtime_checkable
class BudgetView(Protocol):
    """Read-only view of the per-window budget for ``status.budget_used``.

    # TODO(Guard): Guard (a later cross-cutting slice) owns the real budget
    # counter. Until then edithd injects a stub returning 0. This Protocol is
    # the seam; do not build Guard here.
    """

    def budget_used(self) -> int: ...


class ControlServer:
    """Unix-socket JSON-lines Control API server for one ``edithd`` process."""

    def __init__(
        self,
        socket_path: str | Path,
        state: RuntimeState,
        budget: BudgetView,
        on_kill: Callable[[], None],
        on_pause: Callable[[], None] = lambda: None,
        on_resume: Callable[[], None] = lambda: None,
    ) -> None:
        self._path = Path(socket_path)
        self._state = state
        self._budget = budget
        self._on_kill = on_kill
        self._on_pause = on_pause
        self._on_resume = on_resume
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        """Bind the unix socket and begin serving. Sets 0600 perms on the file."""
        # A stale socket from a crashed prior run would make bind() fail; clear it.
        with contextlib.suppress(FileNotFoundError):
            self._path.unlink()
        self._server = await asyncio.start_unix_server(
            self._handle_client, path=str(self._path)
        )
        os.chmod(self._path, _SOCKET_MODE)

    async def stop(self) -> None:
        """Close the listening socket and remove the socket file."""
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        with contextlib.suppress(FileNotFoundError):
            self._path.unlink()

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            # JSON-lines: one request per line, one response per request.
            async for line in reader:
                response = self._dispatch(line)
                writer.write(json.dumps(response).encode() + b"\n")
                await writer.drain()
        except asyncio.CancelledError:
            # Graceful shutdown: let cancellation propagate after we stop reading.
            raise
        except (ConnectionResetError, BrokenPipeError):
            # Client vanished mid-exchange; nothing to do but drop the connection.
            pass
        finally:
            writer.close()
            with contextlib.suppress(ConnectionResetError, BrokenPipeError):
                await writer.wait_closed()

    def _dispatch(self, raw: bytes) -> dict[str, object]:
        try:
            request = json.loads(raw)
        except json.JSONDecodeError:
            return {"ok": False, "error": "malformed JSON"}
        if not isinstance(request, dict):
            return {"ok": False, "error": "request must be a JSON object"}

        cmd = request.get("cmd")
        if cmd == "status":
            return {"ok": True, "status": self._status()}
        if cmd == "pause":
            result = self._transition(self._state.pause)
            if result.get("ok"):
                self._on_pause()
            return result
        if cmd == "resume":
            result = self._transition(self._state.resume)
            if result.get("ok"):
                self._on_resume()
            return result
        if cmd == "kill":
            self._state.kill()
            self._on_kill()
            return {"ok": True}
        return {"ok": False, "error": f"unknown command: {cmd!r}"}

    @staticmethod
    def _transition(action: Callable[[], None]) -> dict[str, object]:
        try:
            action()
        except ValueError as exc:
            # Illegal transition (e.g. pause while STOPPING) -> structured error.
            return {"ok": False, "error": str(exc)}
        return {"ok": True}

    def _status(self) -> dict[str, object]:
        # LOCKED shape (north-star §4.2): exactly these four keys.
        return {
            "state": self._state.state.value,
            "active_skill": self._state.active_skill,
            "budget_used": self._budget.budget_used(),
            "last_event": self._state.last_event,
        }
