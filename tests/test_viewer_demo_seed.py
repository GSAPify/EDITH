"""--demo seeder: produces a dense sample graph the viewer can render."""

from edith.memory.store import MemoryStore
from edith.viewer.demo_seed import seed_demo


def test_seed_demo_populates_dense_graph(tmp_path):
    store = MemoryStore(tmp_path / "mem.kuzu")
    count = seed_demo(store)
    assert count >= 120
    snap = store.graph_snapshot()
    assert len(snap["nodes"]) == count
    # All five demo node types present.
    types = {n["type"] for n in snap["nodes"]}
    assert {"Project", "Repo", "PR", "Person", "Fact"} <= types
    # Dense: plenty of edges, and every link endpoint is a real node.
    ids = {n["id"] for n in snap["nodes"]}
    assert len(snap["links"]) >= 100
    for link in snap["links"]:
        assert link["source"] in ids
        assert link["target"] in ids


def test_seed_demo_is_deterministic(tmp_path):
    a = MemoryStore(tmp_path / "a.kuzu")
    b = MemoryStore(tmp_path / "b.kuzu")
    assert seed_demo(a) == seed_demo(b)
