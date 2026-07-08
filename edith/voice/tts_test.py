"""CLI smoke harness for TTS adapters (spec 03 §Verification).

Usage::

    python -m edith.voice.tts_test --engine piper --text "Hello, EDITH here."
    python -m edith.voice.tts_test --engine elevenlabs --text "Hello from ElevenLabs."

Engine is read from ``--engine`` (or env ``TTS_ENGINE``; default ``piper``).
ElevenLabs credentials are read from env vars ``ELEVENLABS_API_KEY`` and
``ELEVENLABS_VOICE_ID`` — never logged, never hardcoded.

This module is for the owner's LIVE smoke test only; it is NOT unit-tested
(audio output requires real hardware and optional-dependency group ``[voice]``).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys


async def _run(engine: str, text: str) -> None:
    try:
        from edith.voice.adapters import select_adapter
    except ImportError as exc:
        print(
            f"[voice] Cannot import adapters: {exc}\n"
            "Install the voice optional-dependency group:\n"
            "  pip install -e '.[voice]'",
            file=sys.stderr,
        )
        sys.exit(1)

    api_key = os.environ.get("ELEVENLABS_API_KEY", "")
    voice_id = os.environ.get("ELEVENLABS_VOICE_ID", "")

    try:
        adapter = select_adapter(engine, api_key=api_key, voice_id=voice_id)
    except ValueError as exc:
        print(f"[voice] {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"[voice] engine={adapter.name()!r}  text={text!r}")
    try:
        handle = await adapter.speak(text)
        # Piper/ElevenLabs adapters drain internally; nothing to await after speak().
        # stop() is a no-op once playback finishes — call it for a clean exit.
        handle.stop()
        print("[voice] playback complete.")
    except (ImportError, ModuleNotFoundError) as exc:
        print(
            f"[voice] Missing dependency for engine {engine!r}: {exc}\n"
            "Install the voice optional-dependency group:\n"
            "  pip install -e '.[voice]'",
            file=sys.stderr,
        )
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001
        print(f"[voice] speak() failed: {exc}", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="EDITH TTS smoke test")
    parser.add_argument(
        "--engine",
        default=os.environ.get("TTS_ENGINE", "piper"),
        choices=["elevenlabs", "piper"],
        help="TTS engine to exercise (default: $TTS_ENGINE or 'piper')",
    )
    parser.add_argument(
        "--text",
        default="Awaiting your instructions.",
        help="Text to synthesize",
    )
    args = parser.parse_args()
    asyncio.run(_run(args.engine, args.text))


if __name__ == "__main__":
    main()
