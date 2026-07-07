"""graph_snapshot(): force-graph-shaped export of the whole memory graph.

Real embedded Kuzu, no mocks. Seeds a small graph via remember() and asserts
the snapshot's shape, counts, id integrity, and computed degree.
"""

from edith.memory.store import Edge, MemoryStore, Node


def _seed(store: MemoryStore) -> None:
    store.remember(
        nodes=[
            Node("Project", "p1", {"name": "edith", "status": "active"}),
            Node("Repo", "r1", {"path": "~/gitstuff/EDITH", "remote": "git@x/edith"}),
            Node("Person", "person-nate", {"name": "Nate"}),
            Node("Fact", "f1", {"text": "edith uses kuzu", "learned_at": "2026"}),
        ],
        edges=[
            Edge("owns", "Project", "p1", "Repo", "r1"),
            Edge("relates_to", "Fact", "f1", "Project", "p1"),
        ],
    )


def test_snapshot_shape(tmp_path):
    store = MemoryStore(tmp_path / "mem.kuzu")
    _seed(store)
    snap = store.graph_snapshot()
    assert set(snap) == {"nodes", "links"}
    assert isinstance(snap["nodes"], list)
    assert isinstance(snap["links"], list)


def test_snapshot_node_and_link_counts(tmp_path):
    store = MemoryStore(tmp_path / "mem.kuzu")
    _seed(store)
    snap = store.graph_snapshot()
    assert len(snap["nodes"]) == 4
    assert len(snap["links"]) == 2


def test_snapshot_node_fields(tmp_path):
    store = MemoryStore(tmp_path / "mem.kuzu")
    _seed(store)
    snap = store.graph_snapshot()
    by_id = {n["id"]: n for n in snap["nodes"]}
    assert set(by_id) == {"p1", "r1", "person-nate", "f1"}
    proj = by_id["p1"]
    assert proj["type"] == "Project"
    assert proj["label"] == "edith"  # display label from name prop
    assert proj["status"] == "active"  # raw props carried through
    assert "_id" not in proj  # kuzu internal keys stripped
    assert "_label" not in proj


def test_snapshot_link_endpoints_match_node_ids(tmp_path):
    store = MemoryStore(tmp_path / "mem.kuzu")
    _seed(store)
    snap = store.graph_snapshot()
    ids = {n["id"] for n in snap["nodes"]}
    for link in snap["links"]:
        assert link["source"] in ids
        assert link["target"] in ids
        assert "type" in link
    types = {link["type"] for link in snap["links"]}
    assert types == {"owns", "relates_to"}


def test_snapshot_degree_is_computed(tmp_path):
    store = MemoryStore(tmp_path / "mem.kuzu")
    _seed(store)
    snap = store.graph_snapshot()
    by_id = {n["id"]: n for n in snap["nodes"]}
    # p1 touches both edges (owns->r1, f1->relates_to->p1) => degree 2.
    assert by_id["p1"]["degree"] == 2
    assert by_id["r1"]["degree"] == 1
    assert by_id["f1"]["degree"] == 1
    assert by_id["person-nate"]["degree"] == 0


def test_snapshot_empty_graph(tmp_path):
    store = MemoryStore(tmp_path / "mem.kuzu")
    snap = store.graph_snapshot()
    assert snap == {"nodes": [], "links": []}
