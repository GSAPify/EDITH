"""Threaded stdlib HTTP server for the memory viewer.

Routes:
  GET /graph  -> MemoryStore.graph_snapshot() as JSON
  GET /       -> static index.html (and any other path -> static asset)

Binds 127.0.0.1 only (never 0.0.0.0). No web framework, no new deps.
``make_server`` is browser-free and testable; the browser open lives in
``__main__``.
"""

from __future__ import annotations

import json
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from edith.memory.store import MemoryStore

_HOST = "127.0.0.1"
_STATIC_DIR = Path(__file__).parent / "static"


class _ViewerServer(ThreadingHTTPServer):
    """ThreadingHTTPServer that carries the MemoryStore for the handler."""

    def __init__(self, address: tuple[str, int], store: MemoryStore) -> None:
        self.store = store
        super().__init__(address, _ViewerHandler)


class _ViewerHandler(SimpleHTTPRequestHandler):
    """Serves /graph as JSON; delegates everything else to static files."""

    server: _ViewerServer

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, directory=str(_STATIC_DIR), **kwargs)  # type: ignore[arg-type]

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler naming
        if self.path == "/graph":
            self._send_graph()
            return
        super().do_GET()

    def _send_graph(self) -> None:
        body = json.dumps(self.server.store.graph_snapshot()).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        """Silence per-request logging to keep the launcher output clean."""


def make_server(store: MemoryStore, port: int = 8765) -> _ViewerServer:
    """Build (but do not start) a viewer server bound to 127.0.0.1:``port``.

    ``port=0`` binds an ephemeral port; read it back from ``server_address``.
    """
    return _ViewerServer((_HOST, port), store)
