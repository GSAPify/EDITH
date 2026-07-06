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


class RuntimeState:
    """Mutable daemon state. Starts RUNNING; kill is terminal."""

    def __init__(self) -> None:
        self.state: DaemonState = DaemonState.RUNNING
        self.active_skill: str | None = None
        self.last_event: str | None = None

    @property
    def is_paused(self) -> bool:
        """True only while PAUSED — Brain reads this to suspend a pass."""
        return self.state is DaemonState.PAUSED

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
