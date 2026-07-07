"""Viewer HTTP server: real requests against an ephemeral 127.0.0.1 port.

No mocks, no browser. Starts the threaded stdlib server on port 0, GETs
/graph and /, and asserts the JSON shape and static page delivery.
"""

import json
import threading
import urllib.request

from edith.memory.store import Edge, MemoryStore, Node
from edith.viewer.server import make_server


def _seed(store: MemoryStore) -> None:
    store.remember(
        nodes=[
            Node("Project", "p1", {"name": "edith", "status": "active"}),
            Node("Fact", "f1", {"text": "edith uses kuzu"}),
        ],
        edges=[Edge("relates_to", "Fact", "f1", "Project", "p1")],
    )


def _serve(store: MemoryStore):
    server = make_server(store, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[0], server.server_address[1]
    return server, thread, f"http://{host}:{port}"


def test_server_binds_loopback_only(tmp_path):
    store = MemoryStore(tmp_path / "mem.kuzu")
    server = make_server(store, port=0)
    try:
        assert server.server_address[0] == "127.0.0.1"
    finally:
        server.server_close()


def test_graph_endpoint_returns_snapshot_json(tmp_path):
    store = MemoryStore(tmp_path / "mem.kuzu")
    _seed(store)
    server, thread, base = _serve(store)
    try:
        with urllib.request.urlopen(f"{base}/graph", timeout=5) as resp:
            assert resp.status == 200
            assert "application/json" in resp.headers["Content-Type"]
            payload = json.loads(resp.read())
        assert set(payload) == {"nodes", "links"}
        assert len(payload["nodes"]) == 2
        assert len(payload["links"]) == 1
        assert {n["id"] for n in payload["nodes"]} == {"p1", "f1"}
    finally:
        server.shutdown()
        server.server_close()


def test_index_page_served(tmp_path):
    store = MemoryStore(tmp_path / "mem.kuzu")
    _seed(store)
    server, thread, base = _serve(store)
    try:
        with urllib.request.urlopen(f"{base}/", timeout=5) as resp:
            assert resp.status == 200
            body = resp.read().decode()
        assert "<html" in body.lower()
    finally:
        server.shutdown()
        server.server_close()
