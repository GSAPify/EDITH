"""In-session conversation buffer (spec 03 §Conversation memory — literal half).

Brain's durable/semantic memory (Facts recalled from the graph, the exchange
remembered in ``_remember_exchange``) is the *durable* half of cross-turn
context. ``TurnBuffer`` is the *literal* half: an in-RAM rolling window of the
recent conversation, prepended verbatim to the messages Brain assembles so a
follow-up ("and what about X?") still sees what came before.

**The window counts MESSAGES, not exchanges.** ``add`` is called twice per
exchange — once for the owner, once for EDITH — so a buffer of N messages holds
N/2 exchanges. The parameter used to be called ``max_turns`` and defaulted to 6,
which read as "six exchanges" and delivered three: in a live session the owner
referred back to a topic four exchanges old and EDITH had genuinely lost it,
answering that she had no memory of the conversation. Hence the explicit name.

Pure, no I/O. Text is stored as-is — redaction is Brain's job and already runs
upstream (``sanitize_text`` before ``add``), mirroring ``_remember_exchange``.
"""

from __future__ import annotations

from collections import deque

# 2 messages per exchange → 12 exchanges. Sized for how the owner actually talks
# to her: a spoken working session circles back to a topic several exchanges old,
# and the replies are capped at ~40 words by the persona, so the window stays
# cheap in tokens even at this depth.
DEFAULT_MAX_MESSAGES = 24


class TurnBuffer:
    """A rolling buffer of the last ``max_messages`` conversation messages."""

    def __init__(self, max_messages: int = DEFAULT_MAX_MESSAGES) -> None:
        self._turns: deque[dict[str, str]] = deque(maxlen=max_messages)

    def add(self, role: str, text: str) -> None:
        """Append one message; the oldest is evicted once past ``max_messages``."""
        self._turns.append({"role": role, "content": text})

    def messages(self) -> list[dict[str, str]]:
        """Return the buffered messages oldest→newest, ready to splice into a
        chat messages list."""
        return list(self._turns)
