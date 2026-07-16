"""TurnBuffer — the in-session recent-turns buffer (spec 03 §Conversation memory).

A pure rolling window: add appends, oldest evicts past max_turns, messages()
returns the buffered turns oldest→newest in chat-message shape. No I/O; text is
stored as-is (redaction is Brain's job upstream).
"""

from __future__ import annotations

from edith.brain.history import TurnBuffer


def test_messages_empty_by_default() -> None:
    assert TurnBuffer().messages() == []


def test_add_preserves_chronological_order() -> None:
    buf = TurnBuffer()
    buf.add("user", "first")
    buf.add("assistant", "second")
    buf.add("user", "third")

    assert buf.messages() == [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "second"},
        {"role": "user", "content": "third"},
    ]


def test_evicts_oldest_past_max_turns() -> None:
    buf = TurnBuffer(max_turns=2)
    buf.add("user", "a")
    buf.add("assistant", "b")
    buf.add("user", "c")  # evicts "a"

    assert buf.messages() == [
        {"role": "assistant", "content": "b"},
        {"role": "user", "content": "c"},
    ]


def test_default_max_turns_is_six() -> None:
    buf = TurnBuffer()
    for i in range(8):
        buf.add("user", f"turn-{i}")

    messages = buf.messages()
    assert len(messages) == 6  # oldest two evicted
    assert messages[0]["content"] == "turn-2"
    assert messages[-1]["content"] == "turn-7"


def test_text_stored_verbatim() -> None:
    buf = TurnBuffer()
    buf.add("user", "  keep   spacing & punctuation!  ")

    assert buf.messages()[0]["content"] == "  keep   spacing & punctuation!  "
