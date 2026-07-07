"""Person.gh_handle — additive column, safe for the live DB (spec 02 §Data model).

A fresh DB gets ``gh_handle`` in the CREATE; an existing DB gets it via an
idempotent guarded ALTER. Both paths are covered: remember/recall a Person with a
handle on a fresh dir, and construct the store twice on the same dir without error
(the guard must be a no-op the second time).
"""

from __future__ import annotations

from pathlib import Path

from edith.memory.store import MemoryStore, Node


def test_fresh_db_has_gh_handle(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "db")
    store.remember(
        nodes=[Node("Person", "person-tavishi", {"name": "Tavishi", "gh_handle": "tavishi-gh"})]
    )
    hits = store.recall("Tavishi")
    store.close()

    person = next(h for h in hits if h.get("label") == "Person")
    assert person.get("gh_handle") == "tavishi-gh"


def test_migration_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "db"
    first = MemoryStore(db)
    first.close()
    # constructing again on the same dir re-runs _create_schema; the guarded
    # ALTER must be a no-op (column already present) — no error either way.
    second = MemoryStore(db)
    second.remember(
        nodes=[Node("Person", "p1", {"name": "Nate", "gh_handle": "nate-gh"})]
    )
    hits = second.recall("Nate")
    second.close()

    assert any(h.get("gh_handle") == "nate-gh" for h in hits)
