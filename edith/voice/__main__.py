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
import os
import sys

from edith.bus import Event, EventBus
from edith.voice.live import (
    build_live_voice_io,
    resolve_wake_model,
    run_live_loop,
    wake_phrase,
)


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

    # Piper needs a voice model; without one the TTS path fails silently (the
    # runner errors in a background task). Warn clearly rather than mystify.
    if engine == "piper" and not os.environ.get("PIPER_MODEL"):
        print("[voice] WARNING: PIPER_MODEL is not set — Piper TTS won't speak.")
        print("        Download a voice, e.g.:  python -m piper.download_voices en_GB-alan-medium")
        print("        then:  export PIPER_MODEL=/path/to/en_GB-alan-medium.onnx")
        print("        (wake + STT still work; or use --engine elevenlabs)")

    model = resolve_wake_model()
    phrase = wake_phrase(model)
    await voice.speak(f"Voice loop online. Say {phrase} to talk to me.")
    print(f"[voice] wake model: {model}")
    print(f"[voice] listening — say '{phrase}, ...'   (Ctrl-C to stop)")
    try:
        await run_live_loop(voice, wake_model=model)
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
