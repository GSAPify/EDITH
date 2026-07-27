"""``python -m edith.menubar`` — launch the EDITH menu-bar control app.

Owner LIVE-SMOKE only: needs a real macOS menu bar and the ``[menubar]`` optional
extra (``rumps``). Not part of the headless test suite — see
``edith/menubar/controller.py`` for the tested logic and ``app.py`` for the
``rumps`` shell this boots.
"""

from __future__ import annotations

import sys


def main() -> int:
    from edith.menubar.app import main as _main

    # `_main()` builds the rumps.App lazily, so the ImportError for a missing
    # `[menubar]` extra surfaces here, not at the (always-safe) import above.
    try:
        return _main()
    except ImportError as exc:
        print(f"[menubar] cannot start: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
