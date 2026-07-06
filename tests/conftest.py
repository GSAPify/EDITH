"""Test config: gate the live Bifrost smoke test behind --run-live.

Live tests hit the real gateway (cost + network). They are skipped by default
and run only with ``--run-live``. The ``.env`` is loaded ONLY on that path — if
it were loaded unconditionally the billable live call would fire on every plain
``uv run pytest``, breaking both "skipped by default" and the cost rule.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-live",
        action="store_true",
        default=False,
        help="run tests marked `live` against the real Bifrost gateway",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    if config.getoption("--run-live"):
        _load_dotenv()
        return
    skip_live = pytest.mark.skip(reason="needs --run-live (hits the real Bifrost gateway)")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)


def _load_dotenv() -> None:
    """Minimal .env loader for the live path only (no python-dotenv dependency)."""
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())
