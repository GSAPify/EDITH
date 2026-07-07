"""Launcher: ``python -m edith.viewer [--demo] [--port 8765] [--data-dir PATH]``.

Opens the Kuzu DB (live from ``EDITH_DATA_DIR`` or a temp DB with ``--demo``),
serves the graph + static page on 127.0.0.1, and opens the browser. This module
holds the only ``webbrowser.open`` call — the server itself is browser-free.
"""

from __future__ import annotations

import argparse
import os
import tempfile
import webbrowser
from pathlib import Path

from edith.memory.store import MemoryStore
from edith.viewer.demo_seed import seed_demo
from edith.viewer.server import make_server

_DEFAULT_PORT = 8765
_DB_NAME = "memory.kuzu"


def _resolve_db_path(data_dir: str | None) -> Path:
    raw = data_dir or os.environ.get("EDITH_DATA_DIR", "~/.edith/data")
    return Path(raw).expanduser() / _DB_NAME


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m edith.viewer")
    parser.add_argument(
        "--demo", action="store_true", help="seed a temp DB with a sample graph"
    )
    parser.add_argument(
        "--port", type=int, default=_DEFAULT_PORT, help="port to serve on (default 8765)"
    )
    parser.add_argument(
        "--data-dir", default=None, help="override EDITH_DATA_DIR for the live DB"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)

    if args.demo:
        db_path = Path(tempfile.mkdtemp(prefix="edith-viewer-demo-")) / _DB_NAME
        store = MemoryStore(db_path)
        count = seed_demo(store)
        print(f"[viewer] --demo: seeded {count} nodes into {db_path}")
    else:
        db_path = _resolve_db_path(args.data_dir)
        print(f"[viewer] opening live memory DB at {db_path}")
        store = MemoryStore(db_path)

    server = make_server(store, port=args.port)
    host, port = server.server_address[0], server.server_address[1]
    url = f"http://{host}:{port}/"
    print(f"[viewer] serving on {url} (Ctrl-C to stop)")
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[viewer] shutting down")
    finally:
        server.server_close()
        store.close()


if __name__ == "__main__":
    main()
