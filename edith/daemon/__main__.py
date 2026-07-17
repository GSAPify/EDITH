"""``python -m edith.daemon`` — boot the full EDITH daemon with a live voice loop.

This is the composition root (spec 10): it builds the ONE graph-backed ``MemoryStore``
(semantic recall + cross-session ``remember``), the ``Router``, and a real ``VoiceIO`` on a
shared bus, then runs ``EdithDaemon`` with ``enable_voice=True``. Say "Hey Edith, …" and the
daemon's Brain answers by voice with the real graph + all skills (desktop control, PR review,
session query). Owner LIVE-SMOKE only — needs a mic, a speaker, the ``[voice]`` extra, and
(for ``--engine elevenlabs``) an ElevenLabs key.

  python -m edith.daemon --engine piper
  ELEVENLABS_API_KEY=… ELEVENLABS_VOICE_ID=… python -m edith.daemon --engine elevenlabs

Owns the Kuzu handle for its lifetime → do NOT run the viewer/finder/ingest against the same
``memory.kuzu`` while the daemon is up (single-process store).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

import httpx

from edith.bus import EventBus
from edith.daemon.edithd import EdithDaemon, resolve_secrets
from edith.memory.vector import VectorMemoryStore
from edith.router import Router, Tier

_DEFAULT_DATA_DIR = "~/.edith/data"


def _build_router(base_url: str, api_key: str) -> Router:
    models = {
        Tier.HAIKU: os.environ.get("BIFROST_MODEL_HAIKU", "claude-haiku-4-5-20251001"),
        Tier.SONNET: os.environ.get("BIFROST_MODEL_SONNET", "claude-sonnet-4-6"),
        Tier.OPUS: os.environ.get("BIFROST_MODEL_OPUS", "claude-opus-4-8"),
    }
    client = httpx.AsyncClient(base_url=base_url, timeout=30.0)
    return Router(client, api_key, models)


async def _amain(engine: str, data_dir: str) -> int:
    secrets = resolve_secrets()
    if not secrets.bifrost_base_url or not secrets.bifrost_api_key:
        print("[edithd] no Bifrost creds (BIFROST_BASE_URL / BIFROST_API_KEY) — "
              "replies will fail gracefully. Source .env to enable them.")

    # Shared bus so the live VoiceIO publishes voice.utterance onto the bus Brain reads.
    bus = EventBus()
    try:
        from edith.voice.live import build_live_voice_io

        voice = build_live_voice_io(bus, engine=engine)
    except (ImportError, ValueError) as exc:
        print(f"[edithd] cannot start the voice loop: {exc}")
        print("Install the audio stack:  brew install portaudio && uv pip install -e '.[voice]'")
        return 1

    expanded = os.path.expanduser(data_dir)
    store = VectorMemoryStore(os.path.join(expanded, "memory.kuzu"))
    router = _build_router(secrets.bifrost_base_url, secrets.bifrost_api_key)

    daemon = EdithDaemon(
        expanded,
        secrets,
        memory=store,
        router=router,
        bus=bus,
        voice=voice,
        enable_voice=True,
        enable_session_awareness=True,
    )
    await daemon.start()
    print(f"[edithd] running (data={expanded}, engine={engine}). Say 'Hey Edith, …'  "
          "(Ctrl-C to stop)")
    try:
        await daemon.wait_stopped()  # returns on a Control API `kill`
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await daemon.stop()
    print("\n[edithd] stopped.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="edith.daemon", description="EDITH daemon")
    parser.add_argument("--engine", default="piper", choices=["piper", "elevenlabs"])
    parser.add_argument("--data-dir", default=_DEFAULT_DATA_DIR)
    args = parser.parse_args(argv)
    try:
        return asyncio.run(_amain(args.engine, args.data_dir))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
