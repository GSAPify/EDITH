"""``python -m edith.menubar`` — launch the EDITH menu-bar control app.

Owner LIVE-SMOKE only: needs a real macOS menu bar and the ``[menubar]`` optional
extra (``rumps``). Not part of the headless test suite — see
``edith/menubar/controller.py`` for the tested logic and ``app.py`` for the
``rumps`` shell this boots.

``--socket`` / ``--data-dir`` exist because ``edithd`` derives its socket from its own
``--data-dir``; if the daemon is run with a non-default one, the menu bar must be pointed
at the same place or it will poll a dead path and render "not running" indefinitely.
"""

from __future__ import annotations

import argparse
import os
import sys

_SOCKET_BASENAME = "edithd.sock"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="edith.menubar", description="EDITH menu-bar control")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--socket", default=None, help="path to edithd's control socket")
    group.add_argument(
        "--data-dir",
        default=None,
        help="edithd's data dir; the socket is <data-dir>/edithd.sock (matches the daemon)",
    )
    args = parser.parse_args(argv)

    socket_path = args.socket
    if socket_path is None and args.data_dir is not None:
        socket_path = os.path.join(os.path.expanduser(args.data_dir), _SOCKET_BASENAME)

    # Import lazily and catch ImportError around BUILD only: `run()` is the whole app
    # lifetime, and an unrelated lazy ImportError hours in must not be reported as
    # "cannot start". `import edith.menubar.app` itself is always safe (no top-level rumps).
    from edith.menubar.app import build_app

    try:
        app = build_app(socket_path)
    except ImportError as exc:
        print(f"[menubar] cannot start: {exc}")
        return 1

    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
