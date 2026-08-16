# EDITH

**E**ven **D**ead **I**'m **T**he **H**ero. A local-first personal AI presence for macOS.

EDITH is a voice-first, persistent-memory assistant. She watches your dev sessions, remembers your
projects and working style, and takes action on your behalf. Everything runs inside one daemon,
`edithd`.

Local-first is a constraint, not a preference: your graph, your transcripts and your keys stay on the
machine, and the only thing that leaves is a redacted payload to the model gateway. The voice is
Jarvis-style. The system is its own thing.

## Status

Slices 0 through 6 are built, every seam deferred out of them is closed, and the four
operationalization items (launchd, Guard wiring, menu bar, scheduled graph refresh) have landed.
`ruff check edith tests` is clean and the suite is green. Run `pytest` for the current count and read
[`STATE.md`](STATE.md) for what landed most recently.

| # | Slice | State |
|---|-------|-------|
| 0 | North-star architecture | done |
| 1 | Memory + Brain + `edithd` daemon + Control API | done |
| 2 | PR-review skill (first autonomous action, confirm-gated) | done |
| 3 | Voice (wake word + STT + TTS) | core done; live audio needs owner smoke |
| 4 | Session awareness (watches every OMC / Claude Code terminal) | done |
| 5 | Router (tiered model selection + latency masking) | done |
| 6 | Desktop control (launch apps, drive terminals) | core done; OS actions need owner smoke |

Beyond the numbered slices:

| Component | State |
|-----------|-------|
| Daemon composition root: voice to Brain to live graph to speech | done |
| Background reasoning (`think_async`, so opus never blocks a live turn) | core done; voice ping needs owner smoke |
| `Memory.compact()` (bounded conversation-Fact eviction) | done |
| launchd LaunchAgent (`deploy/`), always-on at login | done; `launchctl bootstrap` needs owner smoke |
| Menu-bar control app (`edith/menubar/`) | done; rendering needs owner smoke |
| Guard: autonomy gate + windowed token budget | wired; default-deny in the shipped daemon |
| Weekly graph refresh, in-process behind `--graph-refresh` | done, off by default |
| Memory viewer, repo ingestion, NL finder, workspace graph | done (tooling) |
| Full-duplex audio + echo-cancellation bench | spike, unfinished by design |

What remains is mostly owner live-smoke rather than code. The hardware, GUI and launchd paths cannot
be exercised headlessly, so nothing here proves that EDITH starts under a real launchd session or
that the menu bar renders. The duplex work is the one genuine work-in-progress. Every gap is listed
under [Limitations](#limitations).

## Requirements

- macOS on Apple Silicon, Python 3.11 or newer, and [`uv`](https://github.com/astral-sh/uv)
- A Bifrost gateway key (Pattern's Anthropic-compatible model gateway) for any model call
- For voice: `brew install portaudio`, the `[voice]` extra, and an ElevenLabs key

## Setup

```bash
uv venv --python 3.11 && source .venv/bin/activate
uv pip install -e .              # core
uv pip install -e '.[voice]'     # wake word, STT and TTS (onnxruntime pinned <1.20)
uv pip install --group dev       # pytest, ruff, pyright

cp .env.example .env             # then fill in BIFROST_*
```

Gateway config lives in the gitignored `.env`. The API key belongs in the macOS Keychain via
`keyring`, with a `.env` fallback for development. Nothing secret is logged, published on the bus,
or persisted.

## Running it

The daemon is the real entry point. It builds a live `VoiceIO`, the graph-backed store, the Router,
the background reasoner and every skill on one bus.

```bash
source .venv/bin/activate
set -a; source .env; set +a                    # BIFROST_*, ELEVENLABS_*, EDITH_WAKE_MODEL
lsof -ti tcp:8765 | xargs kill 2>/dev/null     # Kuzu is single-process: free the graph first
python -m edith.daemon --engine elevenlabs
```

Then say "Hey Edith, ...". Ctrl-C stops her.

| Flag | Default | What it is for |
|------|---------|----------------|
| `--engine {piper,elevenlabs}` | `piper` | TTS backend. ElevenLabs needs a key; Piper is local. |
| `--preflight` | off | Probes mic, wake model, speech, gateway and Apple Events, prints what is missing, exits. **Run it first on a new machine or after an OS update:** it provokes each macOS permission prompt where you expect it. |
| `--show-transcript` | off | Echoes heard utterances and spoken answers to stdout. Keep it out of the LaunchAgent, where stdout is an unrotated, unredacted log. |
| `--graph-refresh` | off | Runs the model-free graph refresh in-process, on start and every 7 days of uptime. Never deep-extracts. |
| `--no-session-narration` | off | Use it when "Hey Edith" stops responding while other Claude Code sessions are active. |
| `--data-dir PATH` | `~/.edith/data` | Point at a different graph. |

The last three have sharp edges worth reading once:
[`docs/ENGINEERING-NOTES.md`](docs/ENGINEERING-NOTES.md).

### Always-on (launchd)

The daemon above dies with its terminal. To start it at login and respawn it on crash, follow the
runbook in [`deploy/README.md`](deploy/README.md). Short version:

```bash
mkdir -p ~/.edith/logs && chmod 700 ~/.edith
chmod 600 .env          # the launcher refuses to boot without this
sed -e "s#__EDITH_REPO_DIR__#$HOME/gitstuff/EDITH#g" \
    -e "s#__EDITH_HOME_DIR__#$HOME#g" \
    deploy/com.gsapify.edithd.plist > ~/Library/LaunchAgents/com.gsapify.edithd.plist
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.gsapify.edithd.plist
```

`launchctl bootout gui/$UID/com.gsapify.edithd` stops it, and that is the **only** way to free the
graph for the viewer, finder or ingest. A Control API `kill` is undone immediately by `KeepAlive`,
and `pause` does not release the Kuzu handle at all.

The launcher **sources** `.env`, which executes it, so it refuses to start unless `.env` is mode 600
and owned by you.

### Menu bar

A separate process that talks to the running daemon over its unix socket:

```bash
source .venv/bin/activate      # required: a bare `python` on PATH is a different
                               # interpreter and will not see rumps
uv pip install -e '.[menubar]'
python -m edith.menubar        # --data-dir if the daemon uses a non-default one
```

Pause, resume and kill, plus a status label. `edith/daemon/client.py` drives the same Control API
from the CLI if you prefer that.

### Individual subsystems

Handy for debugging one layer at a time:

```bash
python -m edith.viewer                        # offline local graph viewer (127.0.0.1:8765)
python -m edith.ingest [--dry-run]            # graph from local clones (deep extract, costs model calls)
python -m edith.ingest --workspace patterninc # metadata-graph a whole GitHub org (no clones, no model calls)
python -m edith.ingest --reembed              # embed graph-only Facts (local embedder, free)
python -m edith.finder "seo tools"            # natural-language repo finder with resolve-on-miss
python -m edith.voice --engine elevenlabs     # voice loop only, no daemon or session tap
python -m edith.session                       # session-awareness tap and narration
python -m edith.voice.aec_bench --backend vpio --hardware macbook   # echo-cancellation bench
```

**Only one process may hold the graph at a time.** Kuzu embedded is single-process, so stop the
daemon and the viewer before running ingest or the finder against `~/.edith/data/memory.kuzu`.

The bench is the echo-cancellation spike rather than a shipped subsystem. It needs
`uv pip install -e '.[duplex]'` plus a real mic and speaker, so it is owner-run only. See
[Limitations](#limitations).

## Architecture

```
                          ┌──────────────────────── edithd (daemon) ─────────────────────────┐
                          │                                                                  │
  mic ─► wake ─► STT ─────┼─► voice.utterance ─►┌─────────┐  recall/remember  ┌────────────┐ │
                          │                     │  Brain  │◄─────────────────►│   Memory   │ │
  ~/.claude/projects ─────┼─► session.event ─►  │  (loop) │                   │ Kuzu graph │ │
  (transcript tap)        │   session.state     │ dispatch│   model_call      │ +sqlite-vec│ │
                          │        │            └────┬────┘◄──────┐           └────────────┘ │
                          │        ▼                 │            │                          │
                          │   Narrator ─► speak      ▼            ▼                          │
                          │        │            Skills       ┌────────┐   redacted payload   │
  TTS ◄───────────────────┼────────┘         (PR-review,     │ Router │──────────────────────┼──► Bifrost
                          │                   session query, └───┬────┘   (haiku/sonnet/opus)│    gateway
                          │                   desktop ctrl)      │                           │
                          │                                      ▼                           │
                          │                          BackgroundReasoner ─┐                   │
                          │                          (tracked opus jobs) │                   │
                          │        brain.background_done ◄───────────────┘                   │
                          │                                                                  │
   CLI client ─unix socket──► Control API {pause, resume, kill, status}                       │
   menu bar (edith/menubar) ─┘  same unix socket, separate process                            │
                          └──────────────────────────────────────────────────────────────────┘
```

**Latency shape, which is the whole point of the Router.** Sonnet is the talking voice. Opus never
blocks a spoken turn:

```
  "think about our sharding strategy"
        │
        ├──► SONNET ack NOW ──► speak: "On it, sir. I'll ping you."   turn ends, mic free
        │
        └──► think_async ──► opus off the critical path ──► remember(detail)
                                                       └──► speak: short summary, later
```

Three properties hold across that whole picture:

- **Redaction is a real choke-point.** `sanitize_text` runs inside every `model_call*` and on every
  TTS or bus payload: assignments, provider tokens, PEM blocks, `scheme://user:PASSWORD@host` URIs.
- **The token budget is enforced, and EDITH cannot go mute from exhaustion.** Opus is capped at a
  fraction of the window, so deep thinking and narration are cut first. The live path is never gated.
- **The autonomy gate is default-deny** over the closed desktop vocabulary, with two caveats worth
  knowing before you rely on it.

Everything external sits behind an injectable seam: mic, wake, STT, TTS, `gh`, the model gateway, the
transcript tap, `osascript` and `open`. That is what makes the core headless-testable and leaves
hardware and network as the only owner live-smoke surfaces. All three properties, stated as narrowly
as they are actually true, are in [`docs/ENGINEERING-NOTES.md`](docs/ENGINEERING-NOTES.md).

## Package layout

```
edith/
  bus/        in-process async pub/sub (the Event envelope + EventBus)
  memory/     Kuzu graph + sqlite-vec store, embeddings, secrets choke-point, compact()
  router/     Bifrost adapter, tiering, streaming, latency masking, background reasoning
  brain/      the orchestrator loop (recall, assemble, redact, decide, remember)
  guard/      pure policy: authorize / Decision + windowed token budget (one per daemon)
  daemon/     edithd composition root, Control API (unix socket), RuntimeState, SecureStore
  skills/     Skill contract + gh runner + PRReviewSkill + SessionQuerySkill
  desktop/    command parser + RepoResolver + osascript/open executors (DesktopControlSkill)
  voice/      TTS adapters (ElevenLabs/Piper) + VoiceIO + live wake/STT loop + persona
    duplex/     full-duplex audio spike: macOS VPIO backend + SpeexDSP canceller core
    aec_bench/  headless ERLE, double-talk and latency bench for those backends
  session/    transcript collector + SessionBus + Narrator (session awareness)
  finder/     NL repo finder with resolve-on-miss
  ingest/     repo to graph pipeline (discover, fetch, redact, classify, map) + workspace pass
  viewer/     stdlib local graph viewer
  menubar/    rumps shell over the Control API (optional [menubar] extra)
```

## Development

```bash
pytest                    # full suite
ruff check edith tests    # lint, scoped to library code
pyright                   # types, basic mode
```

Test-first, red to green. Hardware and network live behind injectable seams, and live smokes are
owner-run and recorded per slice: each spec's Completion Record states the exact verification it
shipped with.

Three baseline quirks will fire on a clean checkout and none of them is caused by your change. They
are written up, with the reason each is left alone, in
[`docs/ENGINEERING-NOTES.md`](docs/ENGINEERING-NOTES.md).

## Limitations

Every item here is verified against the code rather than assumed.

- **ASK is unreachable, so the autonomy gate has two verdicts in practice, not three.** Nothing sets
  `needs_confirmation=True`, so nothing can actually ask.
- **No `SIGTERM` handler**, so `launchctl bootout` is an ungraceful stop: `compact()` and
  `MemoryStore.close()` are skipped.
- **EDITH ignores turns during a graph refresh**, for roughly 1.3 minutes, with no reply and not even
  an apology. Deliberate, and it avoids a class of deadlock, but worth knowing.
- **Logs are neither redacted nor rotated.** The redaction choke-point does not run on log records,
  and the LaunchAgent captures stderr permanently. Keep `~/.edith` at mode 700.
- **Kuzu embedded is single-process.** The rule today is "run one thing at a time", and an always-on
  daemon means `bootout` before the viewer, finder or ingest.
- **Full duplex is a spike and the half-duplex gate is still what ships.** Nothing in the voice path
  consumes it, one backend is a canceller core with no backend around it, and one bench metric is
  knowingly wrong.
- **Owner live-smoke is outstanding** for `launchctl bootstrap`, the menu-bar confirm dialog, the
  full voice loop, desktop-control OS actions, background reasoning's spoken ping, and a real
  graph-refresh pass. Seam-tested is not hardware-verified.

Measurements, line references and the reasoning behind each of these are in
[`docs/ENGINEERING-NOTES.md`](docs/ENGINEERING-NOTES.md).

## Where to look

| File | What it is |
|------|------------|
| [`docs/specs/00-north-star.md`](docs/specs/00-north-star.md) | Full architecture. **Read this first.** |
| [`docs/specs/01`-`14`](docs/specs/) | Per-slice specs and Completion Records (01-06 are the numbered slices, 07-14 the follow-ons) |
| [`docs/ENGINEERING-NOTES.md`](docs/ENGINEERING-NOTES.md) | The sharp edges: Guard's real reach, the Kuzu lock, baseline quirks, spike measurements |
| [`deploy/README.md`](deploy/README.md) | launchd install and uninstall runbook, plus the Kuzu-lock consequences |
| [`docs/SESSION-PROTOCOL.md`](docs/SESSION-PROTOCOL.md) | How to resume across sessions (the 90%-context rule) |
| [`STATE.md`](STATE.md) | Current status and what to do next. The resume file. |
| [`BUILD_LOG.md`](BUILD_LOG.md) | Running log of every build session |

## Resuming work

Read `docs/SESSION-PROTOCOL.md`, then `STATE.md`, then the relevant spec. Build test-first. At
session end, or at roughly 90% context, append a Completion Record to the spec, update `STATE.md`
and `BUILD_LOG.md`, then stop.
