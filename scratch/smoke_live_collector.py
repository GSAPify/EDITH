#!/usr/bin/env python3
"""Live smoke: real collector + SessionBus over the owner's REAL transcript tail.

Proves the whole tap pipeline on genuine Claude Code records (not synthetic test dicts):
real file bytes → TranscriptCollector._drain → SessionBus.ingest → session.event/state,
with redaction applied. Deterministic (no timing/buffering): we point the collector at the
newest real transcript, set its offset ~30 KB before EOF, and poll once.

    python scratch/smoke_live_collector.py
"""

from __future__ import annotations

import asyncio
import glob
import os
import re

from edith.bus import Event, EventBus
from edith.memory.secrets import contains_secret
from edith.session.bus import SessionBus
from edith.session.collector import TranscriptCollector

PROJECTS = os.path.expanduser("~/.claude/projects")


async def main() -> None:
    files = glob.glob(os.path.join(PROJECTS, "*", "*.jsonl"))
    if not files:
        print("no real transcripts found")
        return
    newest = max(files, key=os.path.getmtime)
    size = os.path.getsize(newest)

    bus = EventBus()
    events: list[Event] = []
    states: list[Event] = []
    bus.subscribe("session.event", lambda e: events.append(e) or _aw())
    bus.subscribe("session.state", lambda e: states.append(e) or _aw())
    session_bus = SessionBus(bus)

    collector = TranscriptCollector(session_bus.ingest, projects_dir=PROJECTS)
    # Force it to treat the last ~30 KB of the REAL transcript as freshly appended.
    collector._offsets[newest] = max(0, size - 30_000)  # noqa: SLF001 - smoke only

    dispatched = await collector.poll()
    print(f"transcript: {os.path.basename(newest)}  ({size} bytes)")
    print(f"real records dispatched from tail: {dispatched}")
    print(f"session.event published: {len(events)}   session.state published: {len(states)}")

    print("\n-- session.event samples (from REAL records) --")
    for e in events[-8:]:
        p = e.payload
        print(f"  [{p['kind']:8}] repo={p['repo']!s:14} {str(p['summary'])[:80]}")

    # Redaction invariant: strip the [REDACTED] markers (and any trailing marker fragment
    # left by the 160-char summary cap), then assert no RAW secret shape survives in what
    # remains. A redacted credential passes (only the marker is stripped); a surviving raw
    # secret would still trip contains_secret.
    def _stripped(text: str) -> str:
        return re.sub(r"\[REDACTED\]|\[REDACT\w*$", "", text)

    leaks = []
    for e in (*events, *states):
        for field in ("summary", "current_action"):
            text = str(e.payload.get(field) or "")
            if text and contains_secret(_stripped(text)):
                leaks.append((field, e.payload))
    print(f"\nredaction check — summaries with a SURVIVING raw secret: {len(leaks)}")
    assert not leaks, f"LEAK: {leaks[:1]}"
    print("OK: real pasted credentials were redacted; no raw secret survived.")


def _aw() -> asyncio.Future:
    f: asyncio.Future = asyncio.get_event_loop().create_future()
    f.set_result(None)
    return f


if __name__ == "__main__":
    asyncio.run(main())
