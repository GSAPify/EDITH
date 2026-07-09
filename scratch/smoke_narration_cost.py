#!/usr/bin/env python3
"""Measure narration cost (spec 04 verification #5) over the owner's REAL event stream.

Wires a Narrator with a call-counting fake router to the real collector→SessionBus pipeline
and reports: error events / total events, and model_calls / session. If model calls track
error count and errors are common, the narration policy burns budget → must throttle.

    python scratch/smoke_narration_cost.py
"""

from __future__ import annotations

import asyncio
import glob
import os
from collections import Counter

from edith.bus import EventBus
from edith.router import ModelResponse, Tier
from edith.session.bus import SessionBus
from edith.session.collector import TranscriptCollector
from edith.session.narrator import Narrator

PROJECTS = os.path.expanduser("~/.claude/projects")


class _CountingRouter:
    def __init__(self) -> None:
        self.calls = 0

    async def model_call(self, messages, tier_hint: Tier, max_tokens: int = 512) -> ModelResponse:
        self.calls += 1
        return ModelResponse(text="(narration)", input_tokens=0, output_tokens=0)


async def main() -> None:
    files = glob.glob(os.path.join(PROJECTS, "*", "*.jsonl"))
    newest = max(files, key=os.path.getmtime)
    size = os.path.getsize(newest)

    bus = EventBus()
    kinds: Counter[str] = Counter()
    spoken = [0]

    async def _count_events(e) -> None:  # noqa: ANN001
        kinds[str(e.payload.get("kind"))] += 1

    bus.subscribe("session.event", _count_events)

    router = _CountingRouter()

    async def _speak(_t: str) -> None:
        spoken[0] += 1

    Narrator(bus, _speak, router=router)  # no budget_gate → matches edithd default
    session_bus = SessionBus(bus)
    collector = TranscriptCollector(session_bus.ingest, projects_dir=PROJECTS)
    collector._offsets[newest] = max(0, size - 200_000)  # noqa: SLF001 - larger real slice

    await collector.poll()

    total = sum(kinds.values())
    errors = kinds.get("error", 0)
    print(f"total session.event: {total}")
    print(f"kinds: {dict(kinds)}")
    print(f"error events: {errors}  ({100 * errors / max(total, 1):.1f}% of events)")
    print(f"model_calls (narration): {router.calls}")
    print(f"spoken lines: {spoken[0]}")
    print(f"\nverdict: model_calls track errors? {router.calls} vs {errors} errors")


if __name__ == "__main__":
    asyncio.run(main())
