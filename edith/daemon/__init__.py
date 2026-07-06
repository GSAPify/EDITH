"""EDITH ``edithd`` daemon — lifecycle spine + Control API (spec 01 §"edithd
daemon lifecycle", north-star §4.2).

This package is the daemon side only: the runtime state machine, the unix-socket
Control API server, a tiny client, and the async orchestrator that wires the
already-built Bus / Memory / Router / Brain together and brings the Control API
up. It never binds a network port and never auto-loads launchd.
"""

from edith.daemon.state import DaemonState, RuntimeState

__all__ = ["DaemonState", "RuntimeState"]
