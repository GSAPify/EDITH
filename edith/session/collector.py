"""TranscriptCollector — the file-tap half of SessionBus (spec 04 §Step 1).

The Step-0 spike settled the mechanism: tail the Claude Code / OMC session transcripts
under ``~/.claude/projects/<slug>/<sessionId>.jsonl``. They are append-only JSONL, written
per-event within ~1 s, and carry every event class plus owner-pasted content inline.

This is dependency-free by design — a stdlib byte-offset poller, not ``watchdog`` (same
"reuse what we have, no new deps" call as the viewer's stdlib server). A ~1 s poll gives
~1 s narration latency, which the spike showed is well within the transcript's own write
cadence.

**The one invariant these internals get right (the sharp live-only bug):** on ``prime`` the
collector snapshots the current EOF of every pre-existing transcript, so it never replays
months of history as fresh events. Files that appear *after* prime are read from byte 0.
Partial trailing lines (a record mid-flush) are held until their newline arrives.

Redaction is SessionBus's job (``on_record`` → ``SessionBus.ingest`` redacts every field
before anything is published/stored). This collector never logs raw lines — the invariant
"no raw terminal content leaves the process" holds here too.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

_log = logging.getLogger(__name__)

OnRecord = Callable[[dict], Awaitable[None]]

_DEFAULT_PROJECTS = Path.home() / ".claude" / "projects"


class TranscriptCollector:
    """Poll ``~/.claude/projects/**/*.jsonl`` for appended records; dispatch each."""

    def __init__(
        self,
        on_record: OnRecord,
        *,
        projects_dir: str | Path = _DEFAULT_PROJECTS,
        poll_interval: float = 1.0,
    ) -> None:
        self._on_record = on_record
        self._projects = Path(projects_dir)
        self._interval = poll_interval
        # path -> byte offset already consumed (up to and including a newline).
        self._offsets: dict[str, int] = {}

    def _transcripts(self) -> list[Path]:
        if not self._projects.exists():
            return []
        return sorted(self._projects.glob("*/*.jsonl"))

    def prime(self) -> None:
        """Snapshot EOF of existing transcripts so their history is NOT replayed."""
        for path in self._transcripts():
            try:
                self._offsets[str(path)] = path.stat().st_size
            except OSError:
                self._offsets[str(path)] = 0

    async def poll(self) -> int:
        """Dispatch every complete record appended since the last poll. Returns count."""
        dispatched = 0
        for path in self._transcripts():
            dispatched += await self._drain(path)
        return dispatched

    async def _drain(self, path: Path) -> int:
        key = str(path)
        start = self._offsets.get(key, 0)  # unseen file → read from the top
        try:
            size = path.stat().st_size
        except OSError:
            return 0
        if size < start:  # truncated / rotated → restart from the top
            start = 0
        if size <= start:
            self._offsets[key] = size
            return 0

        try:
            with path.open("rb") as f:
                f.seek(start)
                data = f.read(size - start)
        except OSError:
            return 0

        # Only consume up to the last complete line; hold any partial remainder.
        last_nl = data.rfind(b"\n")
        if last_nl == -1:
            return 0  # no complete line yet; do not advance the offset
        self._offsets[key] = start + last_nl + 1

        count = 0
        for raw in data[: last_nl + 1].split(b"\n"):
            if not raw.strip():
                continue
            record = _parse(raw)
            if record is not None:
                await self._on_record(record)
                count += 1
        return count

    async def run(self) -> None:
        """Live loop: prime to EOF, then poll forever. Owner-smoke surface (no mic needed).

        Blocks until cancelled. Priming first is what keeps the daemon from narrating
        the entire pre-existing transcript history the moment it starts.
        """
        self.prime()
        _log.info("session collector up: watching %s", self._projects)
        while True:
            try:
                await self.poll()
            except asyncio.CancelledError:
                raise
            except OSError as exc:  # transient FS error — log (no raw content) and continue
                _log.warning("session collector poll error: %s", exc)
            await asyncio.sleep(self._interval)


def _parse(raw: bytes) -> dict | None:
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return obj if isinstance(obj, dict) else None
