"""In-session recent-turns buffer (spec 03 §Conversation memory — literal half).

Brain's durable/semantic memory (Facts recalled from the graph, the exchange
remembered in ``_remember_exchange``) is the *durable* half of cross-turn
context. ``TurnBuffer`` is the *literal* half: a short in-RAM rolling window of
the last ~6 turns, prepended verbatim to the messages Brain assembles so a tight
follow-up ("and what about X?") still sees the immediately-prior turn.

Pure, no I/O. Text is stored as-is — redaction is Brain's job and already runs
upstream (``sanitize_text`` before ``add``), mirroring ``_remember_exchange``.
"""

from __future__ import annotations

from collections import deque


class TurnBuffer:
    """A rolling buffer of the last ``max_turns`` conversation turns."""

    def __init__(self, max_turns: int = 6) -> None:
        self._turns: deque[dict[str, str]] = deque(maxlen=max_turns)

    def add(self, role: str, text: str) -> None:
        """Append a turn; the oldest is evicted once past ``max_turns``."""
        self._turns.append({"role": role, "content": text})

    def messages(self) -> list[dict[str, str]]:
        """Return the buffered turns oldest→newest, ready to splice into a
        chat messages list."""
        return list(self._turns)
