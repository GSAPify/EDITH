# EDITH Project Memory Guide

## Overview
- EDITH is an always-on, local-first, voice-first personal AI assistant for macOS.
- The production entry point is `python -m edith.daemon`; `launchd` supervises it as `edithd`.
- Python 3.11+ is managed through `uv`; core metadata and dependencies live in `pyproject.toml`.
- Local state lives under `~/.edith/`. Secrets are loaded from macOS Keychain or the gitignored
  `.env` fallback and must never be persisted to memory, logs, or tracked files.

## Architecture
- `edith/daemon/` is the composition root and lifecycle owner. It creates one shared event bus,
  graph-backed memory store, router, guard, voice loop, Brain, skills, session awareness, and the
  unix-socket Control API.
- `edith/bus/` provides in-process async pub/sub between voice, Brain, sessions, and skills.
- `edith/memory/` combines an embedded Kuzu graph with sqlite-vec semantic recall. Kuzu is
  single-process: the daemon, viewer, finder, and ingest tools must not open the live graph
  concurrently.
- `edith/router/` selects model tiers and sends redacted requests through Bifrost. Background
  reasoning stays off the live voice response path.
- `edith/guard/` owns the desktop-action allowlist and windowed token budget. Redaction remains at
  model, TTS, bus, and persistence choke points.
- `edith/voice/` owns wake-word detection, speech-to-text, conversation flow, and pluggable
  ElevenLabs/Piper text-to-speech.
- `edith/session/` tails local Claude/OMC transcripts, normalizes events, and optionally narrates
  session activity.
- `edith/skills/`, `edith/desktop/`, `edith/finder/`, and `edith/ingest/` provide autonomous
  capabilities behind injectable seams.
- `edith/menubar/` is a separate `rumps` process that controls the daemon over
  `~/.edith/data/edithd.sock`.

## User Defined Namespaces
- [Leave blank - user populates]

## Components
- **EdithDaemon** (`edith/daemon/edithd.py`): starts and stops the shared runtime, Control API,
  voice task, session collector, background work, and graph refresh.
- **Daemon CLI** (`edith/daemon/__main__.py`): resolves runtime configuration and constructs the
  live VoiceIO, VectorMemoryStore, Router, Guard, and EdithDaemon.
- **Brain** (`edith/brain/loop.py`): consumes utterances, recalls context, dispatches skills, calls
  the Router, and remembers results.
- **MemoryStore / VectorMemoryStore** (`edith/memory/`): durable graph and vector persistence.
- **Router / BackgroundReasoner** (`edith/router/`): model selection, streaming, usage metering,
  and asynchronous deep reasoning.
- **VoiceIO** (`edith/voice/`): live audio input/output and half-duplex conversation control.
- **Control API** (`edith/daemon/control.py`, `client.py`): JSON-lines commands over a local unix
  socket for status, pause, resume, and kill.
- **LaunchAgent** (`deploy/`): sources the secured `.env`, starts the venv daemon, and keeps it
  alive across crashes and login sessions.

## Patterns
- Keep models, protocols, constants, and shared configuration at module scope; do not define them
  inside functions.
- Follow existing names and module-local patterns before introducing new functions or files.
- Use dependency injection and structural protocols for hardware, network, model, and OS seams.
- Build behavior test-first; use fakes for hardware/network and reserve real mic, GUI, Apple Events,
  and gateway checks for owner live-smokes.
- Use `.venv/bin/python -m pytest`, `ruff check edith tests`, and `pyright` for verification.
- Never run multiple live-graph owners concurrently. Stop the LaunchAgent with
  `launchctl bootout gui/$UID/com.gsapify.edithd` before viewer, finder, or ingest work.
- Do not enable transcript echo in the LaunchAgent; daemon logs are unrotated and not globally
  redacted.
