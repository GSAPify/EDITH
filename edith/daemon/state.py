"""The daemon runtime state machine (spec 01 §"Control API + pause semantics").

One ``RuntimeState`` per ``edithd`` process. It is the single source of truth for
whether the daemon is RUNNING / PAUSED / STOPPING and holds the two mutable
labels (``active_skill``, ``last_event``) the Control API ``status`` command
surfaces to the menu-bar. Brain reads ``is_paused`` to decide whether to skip a
pass (model_call + remember) per the pause semantics.

Transitions:
  - pause  -> PAUSED   (idempotent)
  - resume -> RUNNING  (idempotent)
  - kill   -> STOPPING (terminal — a shutting-down daemon cannot re-enter
              RUNNING/PAUSED; pause/resume from STOPPING raise ValueError)
"""

from __future__ import annotations

from enum import Enum


class DaemonState(Enum):
    """The three daemon lifecycle states. Values serialize into ``status``."""

    RUNNING = "running"
    PAUSED = "paused"
    STOPPING = "stopping"


class VoiceHealth(Enum):
    """Sticky voice-loop health, surfaced via Control API ``status`` (round 2 review).

    Deliberately a DEDICATED field, not derived from ``last_event``: session-narration
    and graph-refresh writes to ``last_event`` are frequent and unrelated, and would
    otherwise silently clear a genuine voice failure the moment either fired next.
    """

    HEALTHY = "healthy"
    FAILED = "failed"


class RuntimeState:
    """Mutable daemon state. Starts RUNNING; kill is terminal."""

    def __init__(self) -> None:
        self.state: DaemonState = DaemonState.RUNNING
        self.active_skill: str | None = None
        self.last_event: str | None = None
        # Sticky voice-loop health (round 2 review): starts HEALTHY even when voice is
        # never wired/enabled — it is only meaningful once EdithDaemon actually starts
        # the live loop, which resets it HEALTHY on every start (see mark_voice_healthy).
        self.voice_health: VoiceHealth = VoiceHealth.HEALTHY

    def mark_voice_healthy(self) -> None:
        """Reset voice health to HEALTHY — called when the live voice loop (re)starts."""
        self.voice_health = VoiceHealth.HEALTHY

    def mark_voice_failed(self) -> None:
        """Mark voice health FAILED — called once, on an unexpected voice-loop exception.

        Sticky: nothing else in this class clears it, so it survives unrelated
        ``last_event`` writes (session narration, graph refresh) until the loop
        genuinely restarts.
        """
        self.voice_health = VoiceHealth.FAILED

    @property
    def is_paused(self) -> bool:
        """True only while PAUSED — Brain reads this to suspend a pass."""
        return self.state is DaemonState.PAUSED

    @property
    def is_stopping(self) -> bool:
        """True once STOPPING — Brain skips a pass so a late utterance never runs
        against subsystems the shutdown path is tearing down (spec 10 review)."""
        return self.state is DaemonState.STOPPING

    def pause(self) -> None:
        """Enter PAUSED. Idempotent. Illegal once STOPPING."""
        self._guard_not_stopping("pause")
        self.state = DaemonState.PAUSED

    def resume(self) -> None:
        """Return to RUNNING. Idempotent. Illegal once STOPPING."""
        self._guard_not_stopping("resume")
        self.state = DaemonState.RUNNING

    def kill(self) -> None:
        """Enter STOPPING. Terminal — graceful shutdown proceeds from here."""
        self.state = DaemonState.STOPPING

    def _guard_not_stopping(self, action: str) -> None:
        if self.state is DaemonState.STOPPING:
            raise ValueError(f"cannot {action} while STOPPING (kill is terminal)")
