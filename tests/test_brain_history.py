"""TurnBuffer — the in-session recent-turns buffer (spec 03 §Conversation memory).

A pure rolling window: add appends, oldest evicts past max_messages, messages()
returns the buffered turns oldest→newest in chat-message shape. No I/O; text is
stored as-is (redaction is Brain's job upstream).
"""

from __future__ import annotations

from edith.brain.history import DEFAULT_MAX_MESSAGES, TurnBuffer


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


def test_evicts_oldest_past_max_messages() -> None:
    buf = TurnBuffer(max_messages=2)
    buf.add("user", "a")
    buf.add("assistant", "b")
    buf.add("user", "c")  # evicts "a"

    assert buf.messages() == [
        {"role": "assistant", "content": "b"},
        {"role": "user", "content": "c"},
    ]


def test_default_window_holds_twelve_exchanges() -> None:
    """The window counts MESSAGES, and add() fires twice per exchange.

    The parameter was called max_turns and defaulted to 6, which reads as six
    exchanges and delivered three. In a live session the owner referred back to a
    topic four exchanges old and EDITH had genuinely lost it — she answered that
    she had no memory of the conversation. This pins the message/exchange ratio so
    the name can't drift back into lying about it.
    """
    buf = TurnBuffer()
    for i in range(12):
        buf.add("user", f"q-{i}")
        buf.add("assistant", f"a-{i}")

    messages = buf.messages()
    assert len(messages) == DEFAULT_MAX_MESSAGES == 24
    assert messages[0]["content"] == "q-0"  # all 12 exchanges still present
    assert messages[-1]["content"] == "a-11"


def test_thirteenth_exchange_evicts_the_first() -> None:
    buf = TurnBuffer()
    for i in range(13):
        buf.add("user", f"q-{i}")
        buf.add("assistant", f"a-{i}")

    messages = buf.messages()
    assert len(messages) == 24
    assert messages[0]["content"] == "q-1"  # q-0/a-0 evicted
    assert messages[-1]["content"] == "a-12"


def test_text_stored_verbatim() -> None:
    buf = TurnBuffer()
    buf.add("user", "  keep   spacing & punctuation!  ")

    assert buf.messages()[0]["content"] == "  keep   spacing & punctuation!  "
