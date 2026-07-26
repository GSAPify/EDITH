# EDITH

**E**ven **D**ead **I**'m **T**he **H**ero — a local-first personal AI presence for macOS.

Voice-first, persistent-memory assistant that watches your dev sessions, remembers your projects
and working style, and takes action on your behalf. Everything runs under the hood in a daemon
(`edithd`).

> Voice is Jarvis-*style*. The system is EDITH — its own thing. Local-first: your graph, your
> transcripts, and your keys never leave the machine except a redacted payload to the model gateway.

## Status

**All numbered slices (0–6) are built, plus every seam that was deferred out of them.**
**348 passed, 2 skipped** (`ruff check edith tests` clean).

What is *not* done is **operationalization**: EDITH runs when you start her in a terminal, and
nothing keeps her memory fresh on a schedule. The "always-on ambient presence" in
`docs/specs/00-north-star.md` is the design target, not today's behavior — see
[Known gaps](#known-gaps) for the honest list.

| # | Slice | State |
|---|-------|-------|
| 0 | North-star architecture | ✅ |
| 1 | Memory + Brain + `edithd` daemon + Control API | ✅ |
| 2 | PR-review skill (first autonomous action, confirm-gated) | ✅ |
| 3 | Voice (wake word + STT + TTS) | ✅ core; live audio = owner smoke |
| 4 | Session awareness (watch every OMC / Claude Code terminal) | ✅ |
| 5 | Router (tiered model selection + latency masking) | ✅ |
| 6 | Desktop control (launch apps, drive terminals) | ✅ core; OS actions = owner smoke |
| — | **Daemon composition root** — voice → Brain → live graph → speak | ✅ |
| — | **Guard** (`authorize` + windowed token budget) | ✅ built; ⚠ injected nowhere yet |
| — | **Background reasoning** (`think_async` — opus never blocks the live turn) | ✅ core; voice ping = owner smoke |
| — | `Memory.compact()` (bounded conv-Fact eviction) | ✅ |
| — | Memory viewer · Repo ingestion · NL finder · Workspace graph | ✅ (tooling) |

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
                          │                   session query, └───┬────┘   (haiku/sonnet/opus)│    gateway
                          │                   desktop ctrl)      │                           │
                          │                                      ▼                           │
                          │                          BackgroundReasoner ─┐                   │
                          │                          (tracked opus jobs) │                   │
                          │        brain.background_done ◄───────────────┘                   │
                          │                                                                  │
   CLI client ─unix socket──► Control API {pause, resume, kill, status}                       │
   (menu-bar app: not built)                                                                  │
                          └──────────────────────────────────────────────────────────────────┘
```

**Latency shape — the whole point of the Router.** Sonnet is the talking voice; opus never blocks
a spoken turn:

```
  "think about our sharding strategy"
        │
        ├──► SONNET ack NOW ──► 🔊 "On it, sir — I'll ping you."   turn ends, mic free
        │
        └──► think_async ──► opus off the critical path ──► remember(detail)
                                                       └──► 🔊 short spoken summary, later
```

**Redaction is a real choke-point today.** `edith/memory/secrets.py::sanitize_text` runs inside
every `model_call*` and on every TTS/bus payload (covers assignments, provider tokens, PEM blocks,
and `scheme://user:PASSWORD@host` URIs). **Guard's other two duties are not yet enforced:**
`edith/guard/guard.py` implements `authorize` + a windowed token budget, but nothing constructs
one, so every `authorize()` / `budget_check` seam still defaults to ALLOW.

Everything is behind **injectable seams** (mic/wake/STT/TTS, `gh`, the model gateway, the
transcript tap, `osascript`/`open`), so the core is headless-testable and the hardware/network
paths are the only owner live-smoke surfaces.

## Requirements

- macOS (Apple Silicon), Python **3.11+**, [`uv`](https://github.com/astral-sh/uv)
- A [Bifrost](https://) gateway key (Pattern's Anthropic-compatible model gateway) for any model call
- Optional, for voice: `brew install portaudio` + the `[voice]` extra + an ElevenLabs key

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
the bus, or persisted.

## Running it

**The daemon is the real entry point** — it builds a live `VoiceIO`, the graph-backed store, the
Router, the background reasoner and every skill on one bus:

```bash
source .venv/bin/activate
set -a; source .env; set +a                    # BIFROST_* + ELEVENLABS_* + EDITH_WAKE_MODEL
lsof -ti tcp:8765 | xargs kill 2>/dev/null     # Kuzu is single-process: free the graph first
python -m edith.daemon --engine elevenlabs     # or --engine piper
```

Then say **"Hey Edith, …"**. Ctrl-C stops her. Pause/resume/kill also work over the Control API
(`edith/daemon/client.py`) — there is no menu-bar app yet.

Individual subsystems still run standalone, which is handy for debugging one layer:

```bash
python -m edith.viewer                        # offline local graph viewer (127.0.0.1:8765)
python -m edith.ingest [--dry-run]            # graph from local clones (deep extract, costs model calls)
python -m edith.ingest --workspace patterninc # metadata-graph a whole GitHub org (no clones, no model calls)
python -m edith.ingest --reembed              # embed graph-only Facts (local embedder, free)
python -m edith.finder "seo tools"            # natural-language repo finder + resolve-on-miss
python -m edith.voice --engine elevenlabs     # voice loop only, no daemon/session tap
python -m edith.session                       # session-awareness tap → narrate (--engine for audio)
```

**Only one process may hold the graph at a time** — Kuzu embedded is single-process, so stop the
daemon (and the viewer) before running ingest or the finder against `~/.edith/data/memory.kuzu`.

## Package layout

```
edith/
  bus/        in-process async pub/sub (the Event envelope + EventBus)
  memory/     Kuzu graph + sqlite-vec store, embeddings, secrets choke-point, compact()
  router/     Bifrost adapter + tiering + streaming + latency masking + background reasoning
  brain/      the orchestrator loop (recall → assemble → redact → decide → remember)
  guard/      pure policy: authorize / Decision + windowed token budget (not yet injected)
  daemon/     edithd composition root, Control API (unix socket), RuntimeState, SecureStore
  skills/     Skill contract + gh runner + PRReviewSkill + SessionQuerySkill
  desktop/    command parser + RepoResolver + osascript/open executors (DesktopControlSkill)
  voice/      TTS adapters (ElevenLabs/Piper) + VoiceIO + live wake/STT loop + persona
  session/    transcript collector + SessionBus + Narrator (session awareness)
  finder/     NL repo finder + resolve-on-miss
  ingest/     repo → graph pipeline (discover → fetch → redact → classify → map) + workspace pass
  viewer/     stdlib local graph viewer
```

## Development

```bash
pytest                    # 348 passed, 2 skipped
ruff check edith tests    # lint (scoped: scripts/sagemaker has 3 pre-existing SIM115)
pyright                   # types (basic mode; see the note below)
```

Test-first (red→green). Hardware/network behind injectable seams; live smokes are owner-run and
documented per slice. See each spec's Completion Record for the exact verification it shipped with.

Two baseline quirks worth knowing before you chase them: `ruff check .` reports **3 pre-existing
SIM115** in `scripts/sagemaker/train_hey_edith.py` (a training script, not library code), and
pyright emits `reportMissingImports` for `httpx`/`keyring` because it is not resolving `.venv` —
neither is caused by your change.

## Known gaps

- **Not actually always-on.** EDITH exists only while a terminal holds `python -m edith.daemon`.
  There is **no launchd job and no `deploy/*.plist`** (`docs/specs/01-memory-brain.md` claims one
  shipped — it never did), no login item. Writing that plist is the top item in `STATE.md`.
- **The graph does not refresh itself.** No cron, no launchd job, no scheduler — every ingest run
  to date was manual, so `memory.kuzu` drifts from reality between runs. Automating it has to
  solve the Kuzu single-process contention with a running `edithd` first.
- **Guard gates nothing yet.** Built and tested (`edith/guard/guard.py`), constructed by nobody, so
  `Narrator.budget_gate`, `Router.budget_check` and `BackgroundReasoner`'s budget check all still
  default to ALLOW. **Desktop control is stricter than that:** `DesktopControlSkill` sets
  `needs_confirmation = False` with **no ASK/DENY branch and no `authorize()` seam at all** — every
  parsed OS action executes. Wiring Guard there means adding the call site, not just injecting.
- **No menu-bar app.** The Control API side exists; the visible surface doesn't.
- **Kuzu embedded is single-process.** The production fix is routing all DB access through
  `edithd`; today it's "only run one thing at a time".
- **Owner live-smoke outstanding** for: the full `edith.daemon` voice loop, desktop-control OS
  actions (Spotify / Terminal / OMC), and background reasoning's spoken ping. The audio and
  OS-side-effect paths are seam-tested, not hardware-verified.

## Where to look

| File | What it is |
|------|------------|
| [`docs/specs/00-north-star.md`](docs/specs/00-north-star.md) | Full architecture. **Read this first.** |
| [`docs/specs/01`–`13`](docs/specs/) | Per-slice specs + Completion Records (01–06 are the numbered slices; 07–13 the follow-ons) |
| [`docs/SESSION-PROTOCOL.md`](docs/SESSION-PROTOCOL.md) | How to resume across sessions (the 90%-context rule) |
| [`STATE.md`](STATE.md) | Current status + what to do next — **the resume file** |
| [`BUILD_LOG.md`](BUILD_LOG.md) | Running log of every build session |

## Resuming work

Read `docs/SESSION-PROTOCOL.md`, then `STATE.md`, then the relevant spec. Build test-first.
At session end (or ~90% context), append a Completion Record to the spec and update `STATE.md` +
`BUILD_LOG.md`, then stop.
