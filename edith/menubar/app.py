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

# Must match how edithd derives its socket: data_dir / _SOCKET_NAME (edithd.py), where
# data_dir comes from --data-dir / EDITH_DATA_DIR. Drift here is invisible: the menu bar
# would poll a dead path and render "not running" forever, indistinguishable from a stopped
# daemon.
#
# Declared locally rather than imported from edithd ON PURPOSE. Importing the daemon module
# for one filename pulls in keyring, kuzu, sqlite3, subprocess and threading — the menu bar
# is a thin unix-socket client and must stay runnable without the daemon's dependency tree.
# (An earlier revision did import it, and `python -m edith.menubar` died on ModuleNotFound:
# keyring the moment it ran outside the fully-provisioned venv.)
#
# Drift is caught at TEST time instead, where importing edithd is free:
# test_socket_basename_matches_the_daemons — asserts this equals edithd._SOCKET_NAME.
_SOCKET_BASENAME = "edithd.sock"
_DEFAULT_DATA_DIR = "~/.edith/data"
_POLL_SECONDS = 3


def default_socket_path() -> str:
    """The socket edithd listens on, honouring ``EDITH_DATA_DIR`` exactly as the daemon does."""
    data_dir = os.environ.get("EDITH_DATA_DIR", _DEFAULT_DATA_DIR)
    return os.path.join(os.path.expanduser(data_dir), _SOCKET_BASENAME)


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


def _become_menu_bar_app() -> None:
    """Force NSApplicationActivationPolicyAccessory so the status item actually renders.

    macOS gives a non-framework Python ``NSApplicationActivationPolicyProhibited`` (2), which
    forbids the process from presenting ANY UI. The status item is still created without error
    — ``NSStatusBar.statusItemWithLength_`` succeeds — and is then silently never drawn. The
    symptom is a healthy process at 0% CPU with empty stderr and nothing in the menu bar.

    ``uv``-managed interpreters are exactly this case: ``python-build-standalone`` reports an
    empty ``PYTHONFRAMEWORK``. Since ``.venv`` here is uv-created, the menu bar could not work
    at all without this. Accessory (1) = visible in the menu bar, absent from the Dock and the
    app switcher, which is precisely what a status-bar app wants.

    Best-effort: a framework build or a bundled .app is already permitted, where this is a
    harmless no-op.
    """
    try:
        from AppKit import NSApplication, NSApplicationActivationPolicyAccessory
    except ImportError:  # pyobjc absent — rumps could not have imported either
        return
    NSApplication.sharedApplication().setActivationPolicy_(
        NSApplicationActivationPolicyAccessory
    )


def build_app(socket_path: str | None = None):
    """Construct the ``rumps.App`` instance. Raises ``ImportError`` if ``rumps`` is missing."""
    rumps = _require_rumps()
    _become_menu_bar_app()
    controller = MenuBarController(ControlClient(socket_path or default_socket_path()))

    class EdithMenuBarApp(rumps.App):
        def __init__(self) -> None:
            # `name` is rumps' internal identity; `title` is what macOS actually draws.
            # Passing the label positionally set only `name`, leaving title=None and
            # icon=None — a status item with neither renders ZERO-WIDTH, so the app ran
            # perfectly and was simply invisible in the menu bar. Always pass title=.
            super().__init__("EDITH", title=NOT_RUNNING_LABEL)
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


def main(socket_path: str | None = None) -> int:
    """Build and run the menu-bar app. Owner LIVE-SMOKE — never called by tests."""
    build_app(socket_path).run()
    return 0
