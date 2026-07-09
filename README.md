# EDITH

**E**ven **D**ead **I**'m **T**he **H**ero — an always-on, local-first personal AI presence for macOS.

Ambient, voice-first, persistent-memory assistant that watches your dev sessions, remembers
your projects and working style, and takes action on your behalf. The only visible surface is a
menu-bar control (pause / resume / kill); everything else runs under the hood in a daemon
(`edithd`).

> Voice is Jarvis-*style*. The system is EDITH — its own thing. Local-first: your graph, your
> transcripts, and your keys never leave the machine except a redacted payload to the model gateway.

## Status

**Building, slice by slice.** Slices 1–4 are shipped and verified; the memory viewer, repo
ingestion, and NL finder are shipped as supporting tooling. **196 tests + 1 live-skipped, ruff +
pyright clean.**

| # | Slice | State |
|---|-------|-------|
| 0 | North-star architecture | ✅ |
| 1 | Memory + Brain + `edithd` daemon + Control API | ✅ |
| 2 | PR-review skill (first autonomous action, confirm-gated) | ✅ |
| 3 | Voice (wake word + STT + TTS) | ✅ core; live audio = owner smoke |
| 4 | Session awareness (watch every OMC / Claude Code terminal) | ✅ |
| 5 | Router (tiered model selection + latency masking) | ⬜ next |
| 6 | Desktop control (launch apps, drive terminals) | ⬜ |
| — | Memory viewer · Repo ingestion · NL finder | ✅ (tooling) |

## Architecture

```
                          ┌──────────────────────── edithd (daemon) ────────────────────────┐
                          │                                                                  │
  🎙 mic ─► wake ─► STT ──┼─► voice.utterance ─►┌─────────┐  recall/remember  ┌────────────┐ │
                          │                     │  Brain  │◄─────────────────►│   Memory   │ │
  ~/.claude/projects ─────┼─► session.event ─►  │  (loop) │                   │ Kuzu graph │ │
  (transcript tap)        │   session.state     │ dispatch│   model_call      │ +sqlite-vec│ │
                          │        │            └────┬────┘◄──────┐           └────────────┘ │
                          │        ▼                 │            │                          │
                          │   Narrator ─► speak      ▼            ▼                          │
                          │        │            Skills       ┌────────┐   redacted payload   │
  🔊 TTS ◄────────────────┼────────┘         (PR-review,     │ Router │──────────────────────┼──► Bifrost
                          │                   session query) └────────┘   (haiku/sonnet/opus)│    gateway
                          │                                                                  │
   menu-bar ──unix socket──► Control API {pause, resume, kill, status}                        │
                          └──────────────────────────────────────────────────────────────────┘

   Guard (redact / authorize / budget) is a cross-cutting CHOKE-POINT applied before any egress.
   Interim: sanitize_text() runs on every model/TTS/bus payload; full Guard is a later slice.
```

Everything is behind **injectable seams** (mic/wake/STT/TTS, `gh`, the model gateway, the
transcript tap), so the core is headless-testable and the hardware/network paths are the only
owner live-smoke surfaces.

## Requirements

- macOS (Apple Silicon), Python **3.11+**, [`uv`](https://github.com/astral-sh/uv)
- A [Bifrost](https://) gateway key (Pattern's Anthropic-compatible model gateway) for any model call
- Optional, for the voice slice: `brew install portaudio` + the `[voice]` extra + an ElevenLabs key

## Setup

```bash
uv venv --python 3.11 && source .venv/bin/activate
uv pip install -e .              # core
uv pip install -e '.[voice]'     # + wake/STT/TTS stack (onnxruntime pinned <1.20)
uv pip install --group dev       # pytest, ruff, pyright

cp .env.example .env             # then fill BIFROST_* (key goes in Keychain in prod)
```

Config + secrets: model-gateway config lives in the gitignored `.env`; the API key belongs in the
macOS Keychain (`keyring`), with a `.env` fallback for dev. Nothing secret is ever logged, put on
the bus, or persisted — `edith/memory/secrets.py::sanitize_text` is the redaction choke-point
(covers assignments, provider tokens, PEM blocks, and `scheme://user:PASSWORD@host` URIs).

## Running it

The subsystems each have a runnable entry (the always-on `edithd` composition root is still a
seam — see Known gaps):

```bash
python -m edith.viewer            # offline local graph viewer (127.0.0.1)
python -m edith.ingest [--dry-run] # populate the graph from local patterninc clones
python -m edith.finder "seo tools" # natural-language repo finder + resolve-on-miss
python -m edith.voice --engine elevenlabs   # live wake→STT→TTS loop (owner smoke; needs mic/key)
python -m edith.session           # live session-awareness tap → narrate (add --engine for audio)
```

Live graph lives at `~/.edith/data/memory.kuzu` (Kuzu is single-process — stop the viewer before
running anything else that opens it).

## Package layout

```
edith/
  bus/        in-process async pub/sub (the Event envelope + EventBus)
  memory/     Kuzu graph + sqlite-vec store, embeddings, secrets choke-point
  router/     Bifrost adapter + Router (single-tier today; Slice 5 adds tiering)
  brain/      the orchestrator loop (recall → assemble → redact → decide → remember)
  daemon/     edithd composition, Control API (unix socket), RuntimeState, SecureStore
  skills/     Skill contract + gh runner + PRReviewSkill + SessionQuerySkill
  voice/      TTS adapters (ElevenLabs/Piper) + VoiceIO + live wake/STT loop
  session/    transcript collector + SessionBus + Narrator (session awareness)
  finder/     NL repo finder + resolve-on-miss
  ingest/     repo → graph ingestion pipeline (discover → fetch → redact → classify → map)
  viewer/     stdlib local graph viewer
```

## Development

```bash
pytest            # 196 passed, 1 skipped
ruff check .      # lint
pyright           # types (basic mode; .venv excluded)
```

Test-first (red→green). Hardware/network behind injectable seams; live smokes are owner-run and
documented per slice. See individual slice specs for the exact verification each shipped with.

## Known gaps

- **No `edithd` composition root yet** — every subsystem runs standalone; nothing constructs a
  real daemon with a live `VoiceIO` + session tap and runs it end-to-end (so EDITH doesn't "talk
  back" to a spoken question yet). This is the integration payoff still to wire.
- **Guard** (real `authorize` / per-window token budget) is deferred; `sanitize_text` + per-feature
  rate limits are the interim.
- Voice **audio** path (mic/speaker/ElevenLabs) is owner live-smoke, not headless-tested.

## Where to look

| File | What it is |
|------|------------|
| [`docs/specs/00-north-star.md`](docs/specs/00-north-star.md) | Full architecture. **Read this first.** |
| [`docs/specs/01`–`06`](docs/specs/) | Per-slice specs (build order 1→6) + Completion Records |
| [`docs/SESSION-PROTOCOL.md`](docs/SESSION-PROTOCOL.md) | How to resume across sessions (the 90%-context rule) |
| [`STATE.md`](STATE.md) | Current slice + per-slice status — **the resume file** |
| [`BUILD_LOG.md`](BUILD_LOG.md) | Running log of every build session |

## Resuming work

Read `docs/SESSION-PROTOCOL.md`, then `STATE.md`, then the current slice's spec. Build test-first.
At session end (or ~90% context), append a Completion Record to the slice spec and update
`STATE.md` + `BUILD_LOG.md`, then stop.
