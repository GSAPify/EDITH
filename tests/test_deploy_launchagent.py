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


def _stub_repo(tmp_path: Path, env_mode: int) -> Path:
    """A minimal repo tree the wrapper can self-locate into, with .env at ``env_mode``."""
    repo = tmp_path / "repo"
    (repo / "deploy").mkdir(parents=True)
    (repo / ".venv" / "bin").mkdir(parents=True)
    env = repo / ".env"
    env.write_text("EDITH_TEST_TOKEN=surfaced-from-dotenv\n")
    env.chmod(env_mode)
    stub_python = repo / ".venv" / "bin" / "python"
    stub_python.write_text('#!/bin/bash\necho "TOKEN=${EDITH_TEST_TOKEN:-MISSING}"\n')
    stub_python.chmod(0o755)
    wrapper = repo / "deploy" / "edithd-launcher.sh"
    wrapper.write_text(_WRAPPER_PATH.read_text())
    wrapper.chmod(0o755)
    return wrapper


def test_wrapper_refuses_a_world_readable_env(tmp_path: Path) -> None:
    """A 0644 .env must abort the boot, not leak the key.

    `source` executes the file, so a writable .env is RCE as the owner at login; a readable
    one hands BIFROST_API_KEY to any other local account. This was live on the build machine
    (.env 0644, a second uid able to traverse to it). Documenting a chmod is not enough —
    without this check the permission silently drifts back and nothing complains.
    """
    wrapper = _stub_repo(tmp_path, 0o644)

    result = subprocess.run(
        ["bash", str(wrapper)], capture_output=True, text=True, check=False, timeout=30
    )

    assert result.returncode == 1, "a world-readable .env must abort the boot"
    assert "refusing to source" in result.stderr
    assert "TOKEN=surfaced-from-dotenv" not in result.stdout  # never sourced


def test_wrapper_accepts_a_correctly_locked_env(tmp_path: Path) -> None:
    """0600 and owned by us — the check must not block the legitimate path."""
    wrapper = _stub_repo(tmp_path, 0o600)

    result = subprocess.run(
        ["bash", str(wrapper)], capture_output=True, text=True, check=False, timeout=30
    )

    assert result.returncode == 0, result.stderr
    assert "TOKEN=surfaced-from-dotenv" in result.stdout


def test_wrapper_execs_the_daemon_module() -> None:
    """The exec target is the daemon — a wrapper that sources .env and runs nothing is useless."""
    assert "-m edith.daemon" in _WRAPPER_PATH.read_text()


def test_wrapper_script_contains_no_credential_looking_value() -> None:
    text = _WRAPPER_PATH.read_text()
    for line in text.splitlines():
        assert not _CREDENTIAL_PATTERN.search(line), (
            f"wrapper script line looks credential-shaped: {line!r}"
        )
