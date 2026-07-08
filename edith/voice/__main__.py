"""``python -m edith.voice`` — live always-listening smoke (spec 03 §Verification).

Boots the real audio loop: say "Hey Jarvis, <something>" and the recognised
transcript prints as it lands on the bus, and a canned line is spoken back so you
hear the TTS path. This is the owner LIVE-SMOKE entry — it needs a mic, a speaker,
the ``[voice]`` extra, and (for ``--engine elevenlabs``) an API key. It is NOT
part of the headless test suite.

  python -m edith.voice --engine piper
  ELEVENLABS_API_KEY=... ELEVENLABS_VOICE_ID=... python -m edith.voice --engine elevenlabs
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from edith.bus import Event, EventBus
from edith.voice.live import build_live_voice_io, run_live_loop


async def _amain(engine: str) -> int:
    bus = EventBus()

    async def _on_utterance(event: Event) -> None:
        text = str(event.payload.get("text", ""))
        confidence = event.payload.get("confidence")
        print(f"[voice.utterance] {text!r}  (confidence={confidence})")

    bus.subscribe("voice.utterance", _on_utterance)

    try:
        voice = build_live_voice_io(bus, engine=engine)
    except (ImportError, ValueError) as exc:
        print(f"[voice] cannot start live loop: {exc}")
        print("Install the audio stack:  brew install portaudio && uv pip install -e '.[voice]'")
        return 1

    await voice.speak("Voice loop online. Say hey Jarvis to talk to me.")
    print("[voice] listening — say 'Hey Jarvis, ...'   (Ctrl-C to stop)")
    try:
        await run_live_loop(voice)
    except KeyboardInterrupt:
        print("\n[voice] stopped.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="edith.voice", description="EDITH live voice loop")
    parser.add_argument("--engine", default="piper", choices=["piper", "elevenlabs"])
    args = parser.parse_args(argv)
    try:
        return asyncio.run(_amain(args.engine))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
