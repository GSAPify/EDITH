"""Follow-up conversation window state machine (spec 03 §Follow-up window).

After EDITH finishes speaking a reply, the mic stays "hot" for a short window
(default 10 s) during which the user can speak a follow-up WITHOUT saying the
wake word. Silence past the deadline returns to normal wake-gated operation.

This module is **pure**: no bus, no audio, no I/O of any kind. The clock is
injected so tests drive time deterministically (same idiom as
``edith.voice.live._gate_action``).

Integration note for the live loop (``edith.voice.live``)
----------------------------------------------------------
The lead wires this into the wake loop. Call sites::

    win = ConversationWindow()

    # 1. After TTS playback fully stops (voice_io.is_speaking goes False):
    win.on_reply_finished()
    #    Called AFTER is_speaking is False so the window never opens while EDITH
    #    is still producing audio — her own TTS tail cannot be captured as a
    #    follow-up because the gate is not yet open.

    # 2. In the wake-detection path, BEFORE requiring a wake phrase:
    if win.accepts_followup():
        # treat the utterance as a follow-up; skip wake-word check
        ...
    else:
        # normal wake-gated path
        ...

    # 3. When a real utterance arrives while CONVERSING (resets/extends deadline):
    win.on_utterance()

    # 4. On barge-in or mute — force back to IDLE immediately:
    win.reset()
"""

from __future__ import annotations

import enum
import time
from collections.abc import Callable


class ConvState(enum.Enum):
    """States of the follow-up conversation window."""

    IDLE = "idle"
    """Wake-gated: only wake-word utterances are accepted."""

    CONVERSING = "conversing"
    """Follow-up mode: utterances are accepted without a wake word until the deadline."""


class ConversationWindow:
    """Pure state machine tracking whether the mic is in follow-up mode.

    Parameters
    ----------
    window_seconds:
        How long (in seconds) to accept follow-ups after a reply finishes.
        Default 10.0 s.
    clock:
        Callable returning the current time as a float (seconds). Injected so
        tests can drive time without sleeping. Defaults to ``time.monotonic``.
    """

    def __init__(
        self,
        window_seconds: float = 10.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._window_seconds = window_seconds
        self._clock = clock
        self._state: ConvState = ConvState.IDLE
        self._deadline: float = 0.0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def state(self) -> ConvState:
        """Current state of the window."""
        return self._state

    def on_reply_finished(self, now: float | None = None) -> None:
        """A reply just finished playing; enter CONVERSING and arm the deadline.

        Called AFTER ``is_speaking`` goes False so the window never opens while
        EDITH is still producing audio — her own TTS tail cannot be captured as
        a follow-up.

        Parameters
        ----------
        now:
            Override the current time (seconds). If None, uses the injected clock.
        """
        t = now if now is not None else self._clock()
        self._deadline = t + self._window_seconds
        self._state = ConvState.CONVERSING

    def accepts_followup(self, now: float | None = None) -> bool:
        """Return True iff currently CONVERSING and the deadline has not passed.

        If the deadline has passed, transitions back to IDLE as a side effect
        before returning False.

        Parameters
        ----------
        now:
            Override the current time (seconds). If None, uses the injected clock.
        """
        if self._state is not ConvState.CONVERSING:
            return False
        t = now if now is not None else self._clock()
        if t >= self._deadline:
            self._state = ConvState.IDLE
            return False
        return True

    def on_utterance(self, now: float | None = None) -> None:
        """A real user utterance arrived while CONVERSING — extend the deadline.

        Resets the deadline to ``now + window_seconds``, keeping the window hot.
        No-op when IDLE (wake-gated mode).

        Parameters
        ----------
        now:
            Override the current time (seconds). If None, uses the injected clock.
        """
        if self._state is not ConvState.CONVERSING:
            return
        t = now if now is not None else self._clock()
        self._deadline = t + self._window_seconds

    def reset(self) -> None:
        """Force back to IDLE immediately (for barge-in or mute)."""
        self._state = ConvState.IDLE
        self._deadline = 0.0
