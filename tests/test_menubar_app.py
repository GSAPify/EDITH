"""Tests for the ``rumps`` shell's optional-dependency boundary (item 3, roadmap).

``rumps`` is an optional extra (``[menubar]``); it is NOT installed in this dev/test
environment. These tests assert the contract from the task brief: importing
``edith.menubar`` (and its ``app`` submodule) must succeed with no ``[menubar]``
extra installed — mirroring ``edith/voice/live.py``'s "heavy imports stay inside
functions" seam — while actually trying to build/run the app without ``rumps``
fails with a clear, actionable message rather than a raw traceback.

Actually launching the app (the real menu bar, the rumps event loop, the confirm
dialog rendering) is owner LIVE-SMOKE only and is not exercised here.
"""

from __future__ import annotations

import importlib

import pytest


def test_import_edith_menubar_package_succeeds_without_rumps():
    importlib.import_module("edith.menubar")


def test_import_edith_menubar_app_succeeds_without_rumps():
    # No module-level `import rumps` — matches edith/voice/live.py's pattern of
    # keeping heavy/optional imports inside functions.
    importlib.import_module("edith.menubar.app")


def test_build_app_without_rumps_raises_actionable_import_error():
    from edith.menubar import app

    with pytest.raises(ImportError, match=r"\[menubar\]"):
        app.build_app()


def test_main_without_rumps_prints_actionable_message_and_returns_nonzero(capsys):
    from edith.menubar.__main__ import main

    exit_code = main()

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "[menubar]" in captured.out
    assert "menubar" in captured.out
