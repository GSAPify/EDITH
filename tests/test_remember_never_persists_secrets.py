"""remember() must never persist a raw secret to the graph.

The never-persist filter runs FIRST in remember (spec §Autonomy & secrets).
A Fact whose text carries a credential is sanitized before write; the raw
secret is absent from the on-disk store afterward.
"""

from edith.memory.store import MemoryStore, Node

_FAKE_SECRET = "GOCSPX-EXAMPLE_FAKE_SECRET_DO_NOT_STORE"


def test_remember_strips_secret_before_writing(tmp_path):
    store = MemoryStore(tmp_path / "mem.kuzu")
    store.remember(
        nodes=[Node("Fact", "f1", {"text": f"owner client_secret: {_FAKE_SECRET}"})]
    )

    # The raw secret must not be anywhere in the recalled fact text.
    hits = store.recall("owner")
    facts = [str(h["text"]) for h in hits if h["label"] == "Fact"]
    assert facts, "the sanitized fact should still be stored"
    assert all(_FAKE_SECRET not in t for t in facts)
    assert any("[REDACTED]" in t for t in facts)
