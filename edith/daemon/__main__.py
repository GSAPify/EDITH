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
from edith.desktop import Intent
from edith.guard import Guard
from edith.memory.vector import VectorMemoryStore
from edith.router import Router, Tier

_DEFAULT_DATA_DIR = "~/.edith/data"


def _build_guard() -> Guard:
    """The daemon's ONE Guard, allowlisted to the desktop control vocabulary.

    Guard's denylist (rm_rf, drop_table, …) is a generic blocklist that is disjoint
    from DesktopControlSkill's four Intents (open_app/spotify/terminal/omc_launch),
    so without this, ``authorize()`` returns ALLOW for every OS action the daemon
    can actually take (spec 11 §Completion Record — the gap this closes). The
    allowlist flips the default: any verb outside the closed desktop vocabulary is
    denied, so a future Intent added without a deliberate Guard decision fails
    closed instead of silently open.
    """
    return Guard(allowlist=frozenset(intent.value for intent in Intent))


def _build_router(client: httpx.AsyncClient, api_key: str, guard: Guard) -> Router:
    # NOTE: Bifrost model ids are duplicated from edith.voice.__main__ — a drift risk
    # (owner guardrail on stale hardcoded constants). TODO(config): centralize a
    # tier→model map both entry points read.
    models = {
        Tier.HAIKU: os.environ.get("BIFROST_MODEL_HAIKU", "claude-haiku-4-5-20251001"),
        Tier.SONNET: os.environ.get("BIFROST_MODEL_SONNET", "claude-sonnet-4-6"),
        Tier.OPUS: os.environ.get("BIFROST_MODEL_OPUS", "claude-opus-4-8"),
    }
    # Guard's two Router touchpoints (spec 11): gate the tier before the call, charge the
    # window after it. ``on_usage`` is what makes ``budget_used`` move at all — without it
    # the budget is theatre.
    return Router(
        client,
        api_key,
        models,
        budget_check=guard.budget_check,
        on_usage=guard.record,
    )


async def _amain(
    engine: str,
    data_dir: str,
    graph_refresh: bool = False,
    session_narration: bool = True,
) -> int:
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
    client = httpx.AsyncClient(base_url=secrets.bifrost_base_url, timeout=30.0)
    # ONE Guard for this daemon process, built BEFORE the Router so the ordering makes the
    # sharing structural: the same instance gates+meters the Router and is handed to the
    # daemon for the reasoner / narrator / desktop / Control API seams.
    guard = _build_guard()
    router = _build_router(client, secrets.bifrost_api_key, guard)

    daemon = EdithDaemon(
        expanded,
        secrets,
        memory=store,
        router=router,
        guard=guard,
        bus=bus,
        voice=voice,
        enable_voice=True,
        enable_session_awareness=session_narration,
        enable_graph_refresh=graph_refresh,
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
        await client.aclose()  # close the HTTP client we opened (no leaked connections)
    print("\n[edithd] stopped.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="edith.daemon", description="EDITH daemon")
    parser.add_argument("--engine", default="piper", choices=["piper", "elevenlabs"])
    parser.add_argument("--data-dir", default=_DEFAULT_DATA_DIR)
    parser.add_argument(
        "--no-session-narration",
        action="store_true",
        help=(
            "stop narrating Claude Code session activity. Narration BLINDS THE WAKE WORD: "
            "the half-duplex mic gate skips wake detection entirely while EDITH speaks, plus "
            "a 2.5s cooldown after. With several active sessions she can talk almost "
            "continuously and never hear 'Hey Edith'. Use this if wake stops responding."
        ),
    )
    parser.add_argument(
        "--graph-refresh",
        action="store_true",
        help=(
            "run the weekly model-free graph refresh in-process (both orgs' metadata pass + a "
            "local reembed). OFF by default. Refreshes on start, then every 7 days of uptime — "
            "there is no persisted last-run timestamp, so a daemon that restarts often re-runs "
            "the ~1.3-min pass each time (idempotent MERGE-upserts, so wasted work not "
            "corruption). Never deep-extracts."
        ),
    )
    args = parser.parse_args(argv)
    try:
        return asyncio.run(
            _amain(
                args.engine,
                args.data_dir,
                args.graph_refresh,
                session_narration=not args.no_session_narration,
            )
        )
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
