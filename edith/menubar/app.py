"""The ``rumps`` status-bar shell (north-star §3 / §4.2).

Thin by design: every branch worth testing (label formatting, command dispatch,
daemon-not-running, a socket that dies mid-session) lives in
:mod:`edith.menubar.controller`; this module only wires a real ``ControlClient``
+ ``MenuBarController`` into ``rumps`` widgets. ``rumps`` is imported INSIDE
functions, never at module level, so ``import edith.menubar.app`` works without
the ``[menubar]`` optional extra installed (mirrors how ``edith/voice/live.py``
isolates ``sounddevice``/``openwakeword`` behind function-local imports).

**Owner LIVE-SMOKE only.** The actual menu bar rendering, the timer-driven poll,
and the kill confirmation dialog need a real macOS session and cannot be
exercised by the headless test suite.
"""

from __future__ import annotations

import asyncio
import os

from edith.daemon.client import ControlClient
from edith.menubar.controller import NOT_RUNNING_LABEL, MenuBarController

# Mirrors edith/daemon/edithd.py's _SOCKET_NAME under the default data dir.
_DEFAULT_SOCKET = os.path.expanduser("~/.edith/data/edithd.sock")
_POLL_SECONDS = 3


def _require_rumps():
    """Import ``rumps``, or raise an actionable ``ImportError`` if it's missing."""
    try:
        import rumps
    except ImportError as exc:
        raise ImportError(
            "the menu-bar app needs the 'rumps' package, which is an optional "
            "dependency. Install it with:\n  uv pip install -e '.[menubar]'"
        ) from exc
    return rumps


def build_app(socket_path: str | None = None):
    """Construct the ``rumps.App`` instance. Raises ``ImportError`` if ``rumps`` is missing."""
    rumps = _require_rumps()
    controller = MenuBarController(ControlClient(socket_path or _DEFAULT_SOCKET))

    class EdithMenuBarApp(rumps.App):
        def __init__(self) -> None:
            super().__init__(NOT_RUNNING_LABEL)
            self.menu = ["Pause", "Resume", "Kill"]

        @rumps.timer(_POLL_SECONDS)
        def _poll(self, _sender: object) -> None:
            self.title = asyncio.run(controller.refresh())

        @rumps.clicked("Pause")
        def _pause(self, _sender: object) -> None:
            asyncio.run(controller.pause())
            self.title = asyncio.run(controller.refresh())

        @rumps.clicked("Resume")
        def _resume(self, _sender: object) -> None:
            asyncio.run(controller.resume())
            self.title = asyncio.run(controller.refresh())

        @rumps.clicked("Kill")
        def _kill(self, _sender: object) -> None:
            # Kill is destructive (stops edithd) — confirm before sending it,
            # same "confirm risky, auto the rest" convention Guard's autonomy
            # gate uses elsewhere in the daemon (north-star §6.3).
            confirmed = rumps.alert(
                title="Kill EDITH?",
                message="This stops the edithd daemon. You'll need to start it again manually.",
                ok="Kill",
                cancel="Cancel",
            )
            if confirmed != 1:
                return
            asyncio.run(controller.kill())
            self.title = asyncio.run(controller.refresh())

    return EdithMenuBarApp()


def main() -> int:
    """Build and run the menu-bar app. Owner LIVE-SMOKE — never called by tests."""
    build_app().run()
    return 0
