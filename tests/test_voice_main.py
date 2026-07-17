"""Tests for the verifiable logic in the voice harness (spec conv-mode §3).

Only ``build_messages`` is unit-tested — the rest of ``edith.voice.__main__`` is the
live-audio shell (mic/speaker/model), which is owner smoke-only. This asserts the
recent-turns buffer is spliced in the right ORDER: system preamble → prior turns →
the new utterance, so the model sees the conversation-so-far then the fresh question.
"""

from __future__ import annotations

from edith.brain.history import TurnBuffer
from edith.voice.__main__ import build_messages


def test_build_messages_empty_buffer_is_system_then_user() -> None:
    msgs = build_messages("PERSONA", TurnBuffer(), "how are you?")
    assert msgs == [
        {"role": "system", "content": "PERSONA"},
        {"role": "user", "content": "how are you?"},
    ]


def test_build_messages_splices_prior_turns_between_system_and_utterance() -> None:
    history = TurnBuffer()
    history.add("user", "who is Nate?")
    history.add("assistant", "Nate is on the platform team, sir.")
    msgs = build_messages("PERSONA", history, "and what is he working on?")

    roles = [m["role"] for m in msgs]
    assert roles == ["system", "user", "assistant", "user"]  # system, prior turn, new question
    assert msgs[0]["content"] == "PERSONA"
    assert msgs[1]["content"] == "who is Nate?"  # the prior turn is present verbatim
    assert msgs[-1]["content"] == "and what is he working on?"  # new utterance is last


def test_build_messages_respects_buffer_cap() -> None:
    history = TurnBuffer(max_turns=2)
    for i in range(4):
        history.add("user", f"turn {i}")
    msgs = build_messages("P", history, "now")
    # system + 2 buffered (the last two) + the new utterance
    assert len(msgs) == 4
    assert msgs[1]["content"] == "turn 2"
    assert msgs[2]["content"] == "turn 3"
