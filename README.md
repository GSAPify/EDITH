# EDITH

**E**ven **D**ead **I**'m **T**he **H**ero — an always-on, local-first personal AI presence for macOS.

Ambient, voice-first, persistent-memory assistant that watches your dev sessions, remembers
your projects and working style, and takes action on your behalf. The only visible surface is a
menu-bar control (pause / resume / kill); everything else runs under the hood in a daemon
(`edithd`).

> Voice is Jarvis-*style*. The system is EDITH — its own thing.

## Status

🚧 **Greenfield — spec phase.** No app code yet. This session's output is the spec set that
future build sessions execute against, one slice at a time.

## Where to look

| File | What it is |
|------|------------|
| [`docs/specs/00-north-star.md`](docs/specs/00-north-star.md) | Full architecture. **Read this first.** |
| [`docs/specs/01`–`06`](docs/specs/) | Per-slice specs (build order 1→6) |
| [`docs/SESSION-PROTOCOL.md`](docs/SESSION-PROTOCOL.md) | How to resume across sessions (the 90%-context rule) |
| [`BUILD_LOG.md`](BUILD_LOG.md) | Running log of every build session |
| [`STATE.md`](STATE.md) | Current slice + per-slice status |

## Build order

1. **Memory + Brain** — the persistent core (deepest spec)
2. **PR-review skill** — first real autonomous action
3. **Voice** — wake word + STT + Jarvis-style TTS
4. **Session awareness** — watch every OMC / Claude Code terminal
5. **Router** — tiered model selection (haiku / sonnet / opus over Bifrost)
6. **Desktop control** — launch apps, drive terminals

## Resuming work

Read `docs/SESSION-PROTOCOL.md`, then `STATE.md`, then the current slice's spec. Build. Log as
you go. At session end (or ~90% context), append a completion record to the slice spec + update
`STATE.md` and `BUILD_LOG.md`, then stop.
