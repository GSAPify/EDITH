"""launchd LaunchAgent template (EDITH operationalization item 1).

``deploy/com.gsapify.edithd.plist`` is a template shipped in the repo, never
auto-loaded, never containing a secret (launchd does not source ``.env`` — see
``deploy/edithd-launcher.sh``, which does that at runtime instead). These tests
are headless-safe: ``plistlib`` is stdlib, and the wrapper is only syntax
checked, never executed (it ``exec``s a venv python and a live daemon).

``launchctl bootstrap`` itself is owner LIVE-SMOKE and is not exercised here.
"""

from __future__ import annotations

import plistlib
import re
import subprocess
from pathlib import Path

import pytest

_DEPLOY_DIR = Path(__file__).resolve().parent.parent / "deploy"
_PLIST_PATH = _DEPLOY_DIR / "com.gsapify.edithd.plist"
_WRAPPER_PATH = _DEPLOY_DIR / "edithd-launcher.sh"

_REQUIRED_KEYS = {
    "Label",
    "ProgramArguments",
    "RunAtLoad",
    "KeepAlive",
    "StandardOutPath",
    "StandardErrorPath",
}

# Credential-shaped strings: long unbroken runs of base64/hex-alphabet
# characters, or a known secret-key prefix. Placeholders like
# "__EDITH_REPO_DIR__" contain underscores and don't match either shape.
_CREDENTIAL_PATTERN = re.compile(
    r"sk-[A-Za-z0-9_-]{16,}"  # Anthropic/OpenAI-style secret key
    r"|[A-Za-z0-9+/]{32,}={0,2}"  # base64 blob
    r"|[0-9a-fA-F]{32,}"  # hex token
)


@pytest.fixture
def plist_data() -> dict[str, object]:
    with _PLIST_PATH.open("rb") as f:
        return plistlib.load(f)


def test_plist_parses_as_valid_xml(plist_data: dict[str, object]) -> None:
    assert isinstance(plist_data, dict)


def test_plist_has_required_keys(plist_data: dict[str, object]) -> None:
    missing = _REQUIRED_KEYS - plist_data.keys()
    assert not missing, f"plist is missing required keys: {missing}"


def test_label_matches_filename(plist_data: dict[str, object]) -> None:
    assert plist_data["Label"] == _PLIST_PATH.stem


def test_run_at_load_and_keep_alive_are_true(plist_data: dict[str, object]) -> None:
    assert plist_data["RunAtLoad"] is True
    assert plist_data["KeepAlive"] is True


def test_log_paths_point_under_edith_home(plist_data: dict[str, object]) -> None:
    for key in ("StandardOutPath", "StandardErrorPath"):
        assert ".edith" in str(plist_data[key]), f"{key} should live under ~/.edith"


def test_program_arguments_invoke_the_wrapper(plist_data: dict[str, object]) -> None:
    args = plist_data["ProgramArguments"]
    assert isinstance(args, list) and args
    assert any("edithd-launcher.sh" in str(arg) for arg in args)


def test_no_environment_variables_key(plist_data: dict[str, object]) -> None:
    """The whole point: secrets must come from the wrapper's ``.env``, not the plist."""
    assert "EnvironmentVariables" not in plist_data


def _flatten_strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        out: list[str] = []
        for v in value.values():
            out.extend(_flatten_strings(v))
        return out
    if isinstance(value, list):
        out = []
        for v in value:
            out.extend(_flatten_strings(v))
        return out
    return []


def test_no_credential_looking_value_in_the_tracked_template(
    plist_data: dict[str, object],
) -> None:
    """The check that actually matters: no secret-shaped string is committed."""
    for value in _flatten_strings(plist_data):
        assert not _CREDENTIAL_PATTERN.search(value), (
            f"plist string looks credential-shaped: {value!r}"
        )


def test_no_credential_in_the_raw_plist_including_xml_comments() -> None:
    """Scan the raw bytes, not just parsed values.

    ``plistlib`` discards XML comments, and a comment is exactly where someone debugging a
    launchd startup would paste a real key ("<!-- try BIFROST_API_KEY=sk-... -->"). The parsed
    scan above cannot see it; this can.
    """
    for line in _PLIST_PATH.read_text().splitlines():
        assert not _CREDENTIAL_PATTERN.search(line), (
            f"raw plist line looks credential-shaped: {line!r}"
        )


def test_wrapper_script_syntax_is_valid() -> None:
    result = subprocess.run(
        ["bash", "-n", str(_WRAPPER_PATH)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_wrapper_actually_sources_env_into_the_daemons_environment(
    tmp_path: Path,
) -> None:
    """RUN the wrapper against a stub repo and prove the .env reached the exec'd process.

    Sourcing ``.env`` is the wrapper's entire reason to exist — launchd inherits no shell
    environment, so if this block is ever dropped the daemon boots with no ``BIFROST_*`` and
    (per ``daemon/__main__.py``) only *warns* before continuing: EDITH comes up, the menu bar
    reads "running", and every reply silently fails.

    A substring check over the file text cannot catch that — the header comment alone contains
    "source", ".env" and "exec". This runs the real script instead: it self-locates its repo
    from ``BASH_SOURCE``, so a stub tree with a fake ``.venv/bin/python`` that echoes the var
    proves the value was exported. Delete the sourcing block and this dies.
    """
    repo = tmp_path / "repo"
    (repo / "deploy").mkdir(parents=True)
    (repo / ".venv" / "bin").mkdir(parents=True)
    (repo / ".env").write_text("EDITH_TEST_TOKEN=surfaced-from-dotenv\n")

    stub_python = repo / ".venv" / "bin" / "python"
    stub_python.write_text('#!/bin/bash\necho "TOKEN=${EDITH_TEST_TOKEN:-MISSING}"\n')
    stub_python.chmod(0o755)

    wrapper = repo / "deploy" / "edithd-launcher.sh"
    wrapper.write_text(_WRAPPER_PATH.read_text())
    wrapper.chmod(0o755)

    result = subprocess.run(
        ["bash", str(wrapper)], capture_output=True, text=True, check=False, timeout=30
    )

    assert result.returncode == 0, result.stderr
    assert "TOKEN=surfaced-from-dotenv" in result.stdout, (
        f"the wrapper did not export .env into the exec'd process: {result.stdout!r}"
    )


def test_wrapper_execs_the_daemon_module() -> None:
    """The exec target is the daemon — a wrapper that sources .env and runs nothing is useless."""
    assert "-m edith.daemon" in _WRAPPER_PATH.read_text()


def test_wrapper_script_contains_no_credential_looking_value() -> None:
    text = _WRAPPER_PATH.read_text()
    for line in text.splitlines():
        assert not _CREDENTIAL_PATTERN.search(line), (
            f"wrapper script line looks credential-shaped: {line!r}"
        )
