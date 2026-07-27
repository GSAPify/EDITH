"""EDITH menu-bar control app (north-star §1 / §3 / §4.2).

The menu bar is the *only* visible surface (north-star §1): pause / resume / kill
plus a status label, talking to ``edithd`` over the existing Control API
(``edith.daemon.client.ControlClient``). This package deliberately mirrors the seam
style ``edith.voice`` uses for its hardware-facing half:

- :mod:`edith.menubar.controller` is the headless-testable half — status polling,
  label formatting, command dispatch, and the "daemon isn't running" / "socket died
  mid-session" paths. No GUI import.
- :mod:`edith.menubar.app` is the thin ``rumps`` shell over the controller. ``rumps``
  is an OPTIONAL dependency (``[menubar]`` extra); importing this package (or
  ``edith.menubar.app``) never requires it — only building/running the actual app
  does, and that fails with an actionable message, not a raw traceback (see
  ``edith/voice/live.py`` for the same pattern with the audio stack).
"""

from __future__ import annotations
