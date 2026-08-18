"""Control API server — asyncio unix-domain-socket, JSON-lines (north-star §4.2).

The menu-bar app (and tests) send one JSON object per line — ``{"cmd": "..."}`` —
and read one JSON object back: ``{"ok": true, ...}`` on success or
``{"ok": false, "error": "..."}`` on failure. The four locked commands are
``pause`` / ``resume`` / ``kill`` / ``status``; ``status`` returns EXACTLY the
LOCKED shape ``{state, active_skill, budget_used, last_event}`` documented in
``docs/specs/00-north-star.md:152`` and ``docs/specs/01-memory-brain.md:788`` — no
more, no less. Sticky voice-loop health (round 2 review) is surfaced through a
separate, opt-in/versioned command, ``status_v2``, which returns those same four
keys plus ``voice_health`` (round 4 review — the original additive-key design
broke the documented exact shape for any client that validates it strictly; the
menu bar continues polling the legacy ``status``, unaffected by this move).

This is socket-only by construction — ``asyncio.start_unix_server`` binds a
filesystem path, never a TCP port (north-star §4.2: "socket only, NEVER a public
network bind"). The socket file is created 0600 (owner-only) so no other local
user can drive the daemon. On ``stop`` the socket file is removed.

``budget_used`` comes from an injected ``BudgetView`` — the daemon's single ``Guard``
(spec 11), so the number the menu bar renders is real spend, not a stub.
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

    ``edith.guard.Guard`` satisfies this structurally and is what edithd now injects
    (spec 11), so ``status.budget_used`` reports the daemon's real window usage. The
    Protocol stays as the seam — the Control API must not depend on Guard's concrete type.
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
        if cmd == "status_v2":
            return {"ok": True, "status": self._status_v2()}
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
        # LOCKED shape (north-star §4.2, spec 01 §788): EXACTLY these four keys —
        # nothing more, nothing less. See _status_v2 for the additive, opt-in
        # surface that carries voice_health (round 4 review).
        return {
            "state": self._state.state.value,
            "active_skill": self._state.active_skill,
            "budget_used": self._budget.budget_used(),
            "last_event": self._state.last_event,
        }

    def _status_v2(self) -> dict[str, object]:
        # Opt-in/versioned command (round 4 review): the same four locked keys as
        # ``status``, plus the sticky ``voice_health`` (round 2 review) — kept off
        # the locked ``status`` command so that wire contract stays exact for any
        # client validating it strictly. RuntimeState's own sticky-health semantics
        # are unchanged; this only changes which command surfaces the field.
        return {**self._status(), "voice_health": self._state.voice_health.value}
