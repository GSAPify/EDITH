"""``python -m edith.voice`` — live always-listening smoke (spec 03 §Verification).

Boots the real audio loop: say "<wake word>, <something>" and EDITH transcribes it and —
when Bifrost creds are present — answers by voice via the Router (Sonnet, EDITH's live
voice). This is the owner LIVE-SMOKE entry: it needs a mic, a speaker, the ``[voice]`` extra,
and (for ``--engine elevenlabs``) an ElevenLabs key. Not part of the headless test suite.

  python -m edith.voice --engine piper
  ELEVENLABS_API_KEY=… ELEVENLABS_VOICE_ID=… python -m edith.voice --engine elevenlabs

Env knobs: ``EDITH_WAKE_MODEL`` (bundled name like ``hey_jarvis`` or a path to a custom
``.onnx``), ``EDITH_WAKE_THRESHOLD`` (default 0.5), ``EDITH_VOICE_DEBUG=1`` (mic-rms +
peak-wake-score heartbeat). Replies need ``BIFROST_BASE_URL`` + ``BIFROST_API_KEY``; without
them the loop still wakes + transcribes + prints, just doesn't answer.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import threading

import httpx

from edith.brain.history import TurnBuffer
from edith.bus import Event, EventBus
from edith.memory.secrets import sanitize_text
from edith.router import Router, Tier
from edith.voice.io import VoiceIO
from edith.voice.live import (
    build_live_voice_io,
    resolve_wake_model,
    run_live_loop,
    wake_phrase,
)

_REPLY_SYSTEM = (
    "You are EDITH, Akhil's personal AI — in the mold of Tony Stark's JARVIS: composed, "
    "precise, dryly witty, never sycophantic. Always address him as 'sir'. He is a senior "
    "AI engineering lead, so be technical and concrete — assume fluency, skip generic "
    "hand-holding and filler like 'how can I help you'. Get straight to the substance. "
    "Your reply is read aloud: one or two crisp spoken sentences, no markdown, no lists."
)


def build_messages(system: str, history: TurnBuffer, text: str) -> list[dict[str, object]]:
    """System preamble + the recent-turns buffer + the new utterance (spec conv-mode §3).

    The buffer gives EDITH the LITERAL prior turns so a follow-up ("and what about X?")
    resolves without the owner re-stating context. Rebuild each turn dict through an
    explicitly-typed literal so the ``list[dict[str, object]]`` target is satisfied
    (a ``dict[str, str]`` is not assignable under invariance). Pure — the one piece of
    harness logic that is unit-tested; everything else here is owner live-smoke.
    """
    messages: list[dict[str, object]] = [{"role": "system", "content": system}]
    messages.extend({"role": t["role"], "content": t["content"]} for t in history.messages())
    messages.append({"role": "user", "content": text})
    return messages


def _start_mute_toggle(voice: VoiceIO) -> None:
    """Owner mute: type ``m``+enter to toggle. Reuses VoiceIO.set_paused (spec conv-mode §4).

    Runs a daemon stdin reader so it never blocks the audio loop. ``set_paused(True)``
    suppresses utterances (voice.wake still fires) — the spec's reuse, not a new surface.
    """
    state = {"muted": False}

    def _loop() -> None:
        for line in sys.stdin:
            if line.strip().lower() in ("m", "mute"):
                state["muted"] = not state["muted"]
                voice.set_paused(state["muted"])
                print(f"[voice] {'MUTED' if state['muted'] else 'live'} — m+enter to toggle")

    threading.Thread(target=_loop, daemon=True).start()


def _build_router() -> Router | None:
    """Build a Router from env, or None when Bifrost creds are absent (print-only mode)."""
    base = os.environ.get("BIFROST_BASE_URL")
    key = os.environ.get("BIFROST_API_KEY")
    if not base or not key:
        return None
    models = {
        Tier.HAIKU: os.environ.get("BIFROST_MODEL_HAIKU", "claude-haiku-4-5-20251001"),
        Tier.SONNET: os.environ.get("BIFROST_MODEL_SONNET", "claude-sonnet-4-6"),
        Tier.OPUS: os.environ.get("BIFROST_MODEL_OPUS", "claude-opus-4-8"),
    }
    client = httpx.AsyncClient(base_url=base, timeout=30.0)
    return Router(client, key, models)


async def _amain(engine: str) -> int:
    bus = EventBus()

    try:
        voice = build_live_voice_io(bus, engine=engine)
    except (ImportError, ValueError) as exc:
        print(f"[voice] cannot start live loop: {exc}")
        print("Install the audio stack:  brew install portaudio && uv pip install -e '.[voice]'")
        return 1

    router = _build_router()
    # In-session recent-turns buffer — the LITERAL half of cross-turn memory (spec
    # conv-mode §3). Semantic/graph recall stays deferred to the edithd composition
    # root (see STATE "daemon-integration gap"); this harness keeps the sir-persona
    # direct call and just remembers the conversation so far.
    history = TurnBuffer()

    async def _on_utterance(event: Event) -> None:
        text = str(event.payload.get("text", ""))
        confidence = event.payload.get("confidence")
        print(f"[voice.utterance] {text!r}  (confidence={confidence})")
        if not text:
            return
        if router is None:
            print("[voice] (no BIFROST creds → not answering; source .env to enable replies)")
            return
        # Router redacts + tier-selects internally (Slice 5); Sonnet is the live voice.
        try:
            reply = await router.model_call(
                build_messages(_REPLY_SYSTEM, history, text),
                Tier.SONNET,
                max_tokens=200,
            )
        except (TimeoutError, httpx.HTTPError) as exc:
            print(f"[voice] model call failed: {exc}")
            await voice.speak("Sorry, I couldn't reach the model just now.")
            return
        print(f"[edith] {reply.text!r}")
        await voice.speak(reply.text)
        # Trail the exchange into the buffer AFTER the reply — redact first (STT text
        # flows straight in), matching Brain's never-persist boundary.
        history.add("user", sanitize_text(text))
        history.add("assistant", sanitize_text(reply.text))

    bus.subscribe("voice.utterance", _on_utterance)
    _start_mute_toggle(voice)

    if engine == "piper" and not os.environ.get("PIPER_MODEL"):
        print("[voice] WARNING: PIPER_MODEL is not set — Piper TTS won't speak.")
        print("        Download a voice, e.g.:  python -m piper.download_voices en_GB-alan-medium")
        print("        then:  export PIPER_MODEL=/path/to/en_GB-alan-medium.onnx")
        print("        (wake + STT still work; or use --engine elevenlabs)")

    model = resolve_wake_model()
    phrase = wake_phrase(model)
    threshold = float(os.environ.get("EDITH_WAKE_THRESHOLD", "0.5"))
    followup = float(os.environ.get("EDITH_FOLLOWUP_SECONDS", "10.0"))
    await voice.speak(f"Voice loop online. Say {phrase} to talk to me.")
    print(f"[voice] wake model: {model}  (threshold {threshold})")
    print(f"[voice] replies: {'ON (Bifrost)' if router else 'OFF (no creds)'}")
    print(f"[voice] conversation mode: follow-ups accepted for {followup:.0f}s after a reply "
          "(no wake word); pauses no longer cut you off")
    print(f"[voice] listening — say '{phrase}, ...'   (m+enter = mute, Ctrl-C to stop)")
    try:
        await run_live_loop(
            voice, wake_model=model, wake_threshold=threshold, followup_seconds=followup
        )
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
