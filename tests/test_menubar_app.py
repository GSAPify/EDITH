"""Tests for the ``rumps`` shell's optional-dependency boundary (item 3, roadmap).

``rumps`` is an optional extra (``[menubar]``). These tests must pass **whether or not it is
installed** — an earlier revision assumed it absent, and the moment the owner followed the
README's own `uv pip install -e '.[menubar]'` the suite HUNG, because `main()` reached
`rumps.App.run()` and blocked on the macOS event loop forever. Anything exercising the missing-
dependency path now forces it deterministically instead of relying on the environment.

These tests assert the contract from the task brief: importing
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


def test_build_app_without_rumps_raises_actionable_import_error(monkeypatch):
    """Force the missing-dependency path rather than depending on rumps being uninstalled."""
    from edith.menubar import app

    def _boom():
        raise ImportError(
            "the menu-bar app needs the 'rumps' package, which is an optional "
            "dependency. Install it with:\n  uv pip install -e '.[menubar]'"
        )

    monkeypatch.setattr(app, "_require_rumps", _boom)
    with pytest.raises(ImportError, match=r"\[menubar\]"):
        app.build_app()


def test_main_without_rumps_prints_actionable_message_and_returns_nonzero(capsys, monkeypatch):
    from edith.menubar import app
    from edith.menubar.__main__ import main

    def _boom(_socket_path=None):
        raise ImportError(
            "the menu-bar app needs the 'rumps' package, which is an optional "
            "dependency. Install it with:\n  uv pip install -e '.[menubar]'"
        )

    monkeypatch.setattr(app, "build_app", _boom)
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
    from edith.menubar import app

    monkeypatch.setenv("EDITH_DATA_DIR", "/Volumes/enc/edith")
    assert app.default_socket_path() == "/Volumes/enc/edith/edithd.sock"

    monkeypatch.delenv("EDITH_DATA_DIR", raising=False)
    assert app.default_socket_path().endswith("/.edith/data/edithd.sock")


def test_main_maps_data_dir_to_the_daemon_socket(monkeypatch):
    """``--data-dir`` must reach ``build_app`` — the override was previously unreachable.

    Captures the argument instead of letting main() construct and RUN a real app: with rumps
    installed, ``.run()`` blocks on the macOS event loop and the suite never returns.
    """
    from edith.menubar import app
    from edith.menubar.__main__ import main

    seen: list[str | None] = []

    class _StubApp:
        def run(self) -> None:  # never starts an event loop
            return None

    def _capture(socket_path=None):
        seen.append(socket_path)
        return _StubApp()

    monkeypatch.setattr(app, "build_app", _capture)

    assert main(["--data-dir", "/Volumes/enc/edith"]) == 0
    assert seen == ["/Volumes/enc/edith/edithd.sock"]


def test_socket_basename_matches_the_daemons() -> None:
    """The menu bar declares the socket name locally; this is what stops it drifting.

    app.py deliberately does NOT import edithd at runtime — doing so drags keyring, kuzu,
    sqlite3 and threading into a process that is only a unix-socket client, and it broke
    `python -m edith.menubar` outside a fully-provisioned venv. Importing edithd HERE is
    free, so the drift check lives in the test suite instead of in the import graph.
    """
    from edith.daemon.edithd import _SOCKET_NAME
    from edith.menubar.app import _SOCKET_BASENAME

    assert _SOCKET_BASENAME == _SOCKET_NAME


def test_menubar_does_not_import_the_daemon_module_at_runtime() -> None:
    """Importing edith.menubar.app must not pull in edithd (and so keyring, kuzu, ...).

    Regression guard for a real failure: `python -m edith.menubar` died with
    ModuleNotFoundError: keyring because app.py imported edithd for one filename constant.
    """
    import subprocess
    import sys

    probe = (
        "import sys, edith.menubar.app; "
        "mods = [m for m in ('edith.daemon.edithd', 'keyring', 'kuzu') if m in sys.modules]; "
        "print(','.join(mods))"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    ).stdout.strip()

    assert out == "", f"edith.menubar.app pulled in heavy modules at import: {out}"
