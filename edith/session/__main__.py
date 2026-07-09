"""``python -m edith.session`` — live session-awareness smoke (spec 04 §Verification).

Boots the real transcript tap against ``~/.claude/projects`` and narrates meaningful
session activity. Open a SECOND terminal, run an OMC / Claude Code session, and watch
EDITH observe it: ``session.event`` / ``session.state`` lines print here as they land,
and (with ``--engine``) start/stop/error/idle transitions are spoken aloud.

  python -m edith.session                       # print-only (no audio)
  python -m edith.session --engine elevenlabs   # + spoken narration (needs [voice] + key)

This is the owner LIVE-SMOKE entry — it needs a real ~/.claude and, for audio, the
``[voice]`` extra. It is NOT part of the headless test suite. Nothing raw is printed:
SessionBus redacts every summary before it reaches the bus (and thus this printer).
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from edith.bus import Event, EventBus
from edith.session.bus import SessionBus
from edith.session.collector import TranscriptCollector
from edith.session.narrator import Narrator

_IDLE_TICK_SECONDS = 30.0


async def _amain(engine: str | None) -> int:
    bus = EventBus()

    async def _show(event: Event) -> None:
        p = event.payload
        repo = str(p.get("repo") or "-")
        if event.topic == "session.event":
            print(f"[event] {p.get('kind'):8} {repo:14} {p.get('summary')}", flush=True)
        else:
            act = p.get("current_action") or ""
            print(f"[state] {p.get('state'):8} {repo:14} {act}", flush=True)

    bus.subscribe("session.event", _show)
    bus.subscribe("session.state", _show)

    speak = await _build_speak(bus, engine)
    narrator = Narrator(bus, speak)  # one instance: narrates events AND drives idle ticks

    session_bus = SessionBus(bus)
    collector = TranscriptCollector(session_bus.ingest)

    if engine:
        await speak("Session awareness online. I'm watching your terminals.")
    print("[session] watching ~/.claude/projects — run OMC in another window.", flush=True)
    print("[session] Ctrl-C to stop.", flush=True)

    async def _idle_loop() -> None:
        while True:
            await asyncio.sleep(_IDLE_TICK_SECONDS)
            await narrator.tick()

    try:
        await asyncio.gather(collector.run(), _idle_loop())
    except KeyboardInterrupt:
        print("\n[session] stopped.", flush=True)
    return 0


async def _build_speak(bus: EventBus, engine: str | None):
    """A speak callable: real VoiceIO TTS when --engine is given, else print-only."""
    if not engine:
        async def _print_speak(text: str) -> None:
            print(f"🔊 {text}", flush=True)

        return _print_speak

    # Lazy: only import the voice stack when an engine is actually requested.
    from edith.voice.live import build_live_voice_io

    voice = build_live_voice_io(bus, engine=engine)
    return voice.speak


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="edith.session", description="EDITH session awareness")
    parser.add_argument("--engine", default=None, choices=["piper", "elevenlabs"])
    args = parser.parse_args(argv)
    try:
        return asyncio.run(_amain(args.engine))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
