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

    exit_code = main([])

    assert exit_code == 1
    captured = capsys.readouterr()
    # Assert the INSTRUCTION, not the "[menubar]" log prefix — matching the prefix passes
    # even if the message is gutted to "rumps not installed", losing the one thing the
    # user needs. The install command is what makes it actionable.
    assert "uv pip install" in captured.out
    assert ".[menubar]" in captured.out


def test_default_socket_path_tracks_the_daemons_derivation(monkeypatch):
    """The menu bar must resolve the same socket edithd binds, not a re-spelled literal.

    edithd builds ``data_dir / _SOCKET_NAME`` from its ``--data-dir``. A hardcoded
    ``~/.edith/data/edithd.sock`` here would silently poll a dead path the moment the owner
    runs the daemon elsewhere, and ``refresh()`` renders that identically to "not running" —
    a misconfiguration indistinguishable from a stopped daemon, forever.
    """
    from edith.daemon.edithd import _SOCKET_NAME
    from edith.menubar import app

    monkeypatch.setenv("EDITH_DATA_DIR", "/Volumes/enc/edith")
    assert app.default_socket_path() == f"/Volumes/enc/edith/{_SOCKET_NAME}"

    monkeypatch.delenv("EDITH_DATA_DIR", raising=False)
    assert app.default_socket_path().endswith(f"/.edith/data/{_SOCKET_NAME}")


def test_main_maps_data_dir_to_the_daemon_socket(capsys):
    """``--data-dir`` must reach ``build_app`` — the override was previously unreachable."""
    from edith.menubar.__main__ import main

    # rumps is absent, so build_app raises; the message proves we got that far with our path.
    assert main(["--data-dir", "/Volumes/enc/edith"]) == 1
    assert "[menubar] cannot start" in capsys.readouterr().out
