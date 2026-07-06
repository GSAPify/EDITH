"""Graph-only Memory store: schema, remember(), recall() via Cypher traversal.

Uses real embedded Kuzu (no mocks). Each test gets an isolated DB under tmp_path.
No network / no embeddings here — that is exercised in the vector tests.
"""

from edith.memory.store import Edge, MemoryStore, Node


def test_schema_created_on_open(tmp_path):
    store = MemoryStore(tmp_path / "mem.kuzu")
    # The core node tables from the spec's data model must exist after open.
    tables = store.node_tables()
    assert {"Owner", "Project", "Repo", "Person", "Fact"} <= tables


def test_remember_and_recall_a_fact_by_project(tmp_path):
    store = MemoryStore(tmp_path / "mem.kuzu")
    store.remember(
        nodes=[
            Node("Project", "proj-onboarding", {"name": "onboarding-portal", "status": "active"}),
            Node(
                "Fact",
                "fact-1",
                {"text": "Unknown object = service account not shared on template"},
            ),
        ],
        edges=[Edge("relates_to", "Fact", "fact-1", "Project", "proj-onboarding")],
    )

    hits = store.recall("onboarding-portal")
    texts = [str(h["text"]) for h in hits if h["label"] == "Fact"]
    assert any("service account not shared" in t for t in texts)


def test_recall_survives_reopen(tmp_path):
    db_path = tmp_path / "mem.kuzu"
    store = MemoryStore(db_path)
    store.remember(
        nodes=[
            Node("Project", "p1", {"name": "edith", "status": "active"}),
            Node("Fact", "f1", {"text": "edith uses kuzu for memory"}),
        ],
        edges=[Edge("relates_to", "Fact", "f1", "Project", "p1")],
    )
    store.close()

    # Fresh process-like reopen of the same on-disk DB (the recall-across-restart promise).
    reopened = MemoryStore(db_path)
    hits = reopened.recall("edith")
    assert any("uses kuzu" in str(h["text"]) for h in hits if h["label"] == "Fact")


def test_remember_is_idempotent_on_node_id(tmp_path):
    store = MemoryStore(tmp_path / "mem.kuzu")
    node = Node("Person", "person-nate", {"name": "Nate"})
    store.remember(nodes=[node])
    store.remember(nodes=[node])  # second write of same id must not raise / duplicate
    assert store.count("Person") == 1
