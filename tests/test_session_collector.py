"""TranscriptCollector (spec 04 §Step 1): tail ~/.claude/projects transcripts.

The one thing unit tests MUST pin down (the sharp live-only bug): on startup the
collector seeks to EOF of pre-existing transcripts and only dispatches *newly
appended* lines — otherwise it replays entire multi-MB histories as fresh events
and fires model-gated narrations on months-old activity. These tests use a tmp
projects dir; no real ~/.claude, no network.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from edith.session.collector import TranscriptCollector

pytestmark = pytest.mark.asyncio


class _Recorder:
    def __init__(self) -> None:
        self.records: list[dict] = []

    async def __call__(self, record: dict) -> None:
        self.records.append(record)


def _proj(tmp_path: Path, session: str) -> Path:
    d = tmp_path / "-Users-x-gitstuff-agents"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{session}.jsonl"


def _append(path: Path, *records: dict) -> None:
    with path.open("a") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _rec(text: str) -> dict:
    return {"type": "user", "sessionId": "s1", "promptSource": "typed",
            "message": {"role": "user", "content": text}}


async def test_history_before_prime_is_not_replayed(tmp_path: Path) -> None:
    f = _proj(tmp_path, "s1")
    _append(f, _rec("old one"), _rec("old two"), _rec("old three"))

    sink = _Recorder()
    c = TranscriptCollector(sink, projects_dir=tmp_path)
    c.prime()
    n = await c.poll()

    assert n == 0
    assert sink.records == []


async def test_appended_after_prime_is_dispatched(tmp_path: Path) -> None:
    f = _proj(tmp_path, "s1")
    _append(f, _rec("old"))
    sink = _Recorder()
    c = TranscriptCollector(sink, projects_dir=tmp_path)
    c.prime()

    _append(f, _rec("fresh one"), _rec("fresh two"))
    n = await c.poll()

    assert n == 2
    texts = [r["message"]["content"] for r in sink.records]
    assert texts == ["fresh one", "fresh two"]


async def test_new_file_after_prime_read_from_start(tmp_path: Path) -> None:
    sink = _Recorder()
    c = TranscriptCollector(sink, projects_dir=tmp_path)
    c.prime()  # no files yet

    f = _proj(tmp_path, "s2")
    _append(f, _rec("brand new session line"))
    n = await c.poll()

    assert n == 1
    assert sink.records[0]["message"]["content"] == "brand new session line"


async def test_partial_line_waits_for_newline(tmp_path: Path) -> None:
    f = _proj(tmp_path, "s1")
    sink = _Recorder()
    c = TranscriptCollector(sink, projects_dir=tmp_path)
    c.prime()

    # Write a record WITHOUT a trailing newline (mid-write flush).
    with f.open("a") as fh:
        fh.write(json.dumps(_rec("half")))
    assert await c.poll() == 0  # incomplete line not consumed

    # Complete the line; now it dispatches exactly once.
    with f.open("a") as fh:
        fh.write("\n")
    assert await c.poll() == 1
    assert sink.records[0]["message"]["content"] == "half"


async def test_malformed_json_is_skipped(tmp_path: Path) -> None:
    f = _proj(tmp_path, "s1")
    sink = _Recorder()
    c = TranscriptCollector(sink, projects_dir=tmp_path)
    c.prime()

    with f.open("a") as fh:
        fh.write("not json at all\n")
        fh.write(json.dumps(_rec("valid")) + "\n")
    n = await c.poll()

    assert n == 1
    assert sink.records[0]["message"]["content"] == "valid"
