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


def test_wrapper_script_syntax_is_valid() -> None:
    result = subprocess.run(
        ["bash", "-n", str(_WRAPPER_PATH)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_wrapper_script_sources_env_and_execs_daemon() -> None:
    text = _WRAPPER_PATH.read_text()
    assert "source" in text and ".env" in text
    assert "exec" in text and "edith.daemon" in text


def test_wrapper_script_contains_no_credential_looking_value() -> None:
    text = _WRAPPER_PATH.read_text()
    for line in text.splitlines():
        assert not _CREDENTIAL_PATTERN.search(line), (
            f"wrapper script line looks credential-shaped: {line!r}"
        )
