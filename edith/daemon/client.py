"""Control API client — one request, one response over the unix socket.

A tiny helper the tests use now and the menu-bar app will use later. It opens a
unix-socket connection, writes one JSON-lines request, reads one JSON-lines
response, and closes. No retry, no pooling — the Control API is a local,
low-frequency command channel; a failed connect is an ``OSError`` the caller
sees immediately rather than a hang.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path


class ControlClient:
    """Send a single Control API command over the unix socket and read the reply."""

    def __init__(self, socket_path: str | Path) -> None:
        self._path = str(socket_path)

    async def send(self, request: dict[str, object]) -> dict[str, object]:
        """Connect, send one JSON-lines request, return the parsed JSON response."""
        reader, writer = await asyncio.open_unix_connection(path=self._path)
        try:
            writer.write(json.dumps(request).encode() + b"\n")
            await writer.drain()
            line = await reader.readline()
        finally:
            writer.close()
            await writer.wait_closed()
        return json.loads(line)
