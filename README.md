# EDITH

**E**ven **D**ead **I**'m **T**he **H**ero — a local-first personal AI presence for macOS.

Voice-first, persistent-memory assistant that watches your dev sessions, remembers your projects
and working style, and takes action on your behalf. Everything runs under the hood in a daemon
(`edithd`).

> Voice is Jarvis-*style*. The system is EDITH — its own thing. Local-first: your graph, your
> transcripts, and your keys never leave the machine except a redacted payload to the model gateway.

## Status

**All numbered slices (0–6) are built, every seam deferred out of them is closed, and the four
operationalization items — launchd, Guard wiring, menu bar, scheduled refresh — have landed.**
**416 passed, 2 skipped** (`ruff check edith tests` clean).

What remains is **owner live-smoke**, not code. The hardware, GUI and launchd paths cannot be
exercised headlessly, so nothing below proves EDITH starts under a real launchd session or that
the menu bar renders. See [Known gaps](#known-gaps) for the honest list.

| # | Slice | State |
|---|-------|-------|
| 0 | North-star architecture | done |
| 1 | Memory + Brain + `edithd` daemon + Control API | done |
| 2 | PR-review skill (first autonomous action, confirm-gated) | done |
| 3 | Voice (wake word + STT + TTS) | core done; live audio = owner smoke |
| 4 | Session awareness (watch every OMC / Claude Code terminal) | done |
| 5 | Router (tiered model selection + latency masking) | done |
| 6 | Desktop control (launch apps, drive terminals) | core done; OS actions = owner smoke |
| — | Daemon composition root — voice to Brain to live graph to speech | done |
| — | Background reasoning (`think_async` — opus never blocks the live turn) | core done; voice ping = owner smoke |
| — | `Memory.compact()` (bounded conv-Fact eviction) | done |
| — | launchd LaunchAgent (`deploy/`) — always-on at login | done; `launchctl bootstrap` = owner smoke |
| — | Menu-bar control app (`edith/menubar/`) | done; rendering = owner smoke |
| — | Guard (`authorize` + windowed token budget) | wired; **gate is inert by default** — see below |
| — | Weekly graph refresh (in-process, `--graph-refresh`) | done, off by default |
| — | Memory viewer · Repo ingestion · NL finder · Workspace graph | done (tooling) |

**Read the Guard row carefully.** The budget half is real and metering: every `model_call*`
charges `Guard.record`, and opus is cut before Sonnet/Haiku so the live voice degrades last. The
**authorization** half is not. `Guard`'s default denylist (`rm_rf`, `drop_table`, …) and the
desktop `Intent` vocabulary (`open_app`, `spotify`, `terminal`, `omc_launch`) are disjoint sets,
so `authorize()` returns ALLOW for every OS action the daemon can actually take. Do not read
"Guard is wired" as "OS actions are gated" — they are not.

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

**Latency shape — the whole point of the Router.** Sonnet is the talking voice; opus never blocks
a spoken turn:

```
  "think about our sharding strategy"
        │
        ├──► SONNET ack NOW ──► speak: "On it, sir — I'll ping you."   turn ends, mic free
        │
        └──► think_async ──► opus off the critical path ──► remember(detail)
                                                       └──► speak: short summary, later
```

**Redaction is a real choke-point today.** `edith/memory/secrets.py::sanitize_text` runs inside
every `model_call*` and on every TTS/bus payload (covers assignments, provider tokens, PEM blocks,
and `scheme://user:PASSWORD@host` URIs). **Guard's budget is now enforced too:** the daemon
constructs one `Guard` and injects it into the Router, the background reasoner, the Narrator and
the Control API's status view. Every `model_call*` charges `Guard.record`, and `budget_check` is
tier-aware — opus is capped at a fraction of the window so it is cut off before Sonnet and Haiku.
The practical guarantee is that **EDITH cannot go mute from budget exhaustion**: she loses deep
thinking and spoken narration first, and the live conversational path is never gated.

**Guard's `authorize` is wired but currently decides nothing** — see the Status note above.

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

Then say **"Hey Edith, …"**. Ctrl-C stops her.

Add `--graph-refresh` to also run the weekly model-free graph refresh in-process. It is **off by
default**. It refreshes on start and then every 7 days of *uptime* — an interval, not a calendar
schedule — and there is no persisted last-run timestamp, so a machine that reboots daily gets a
daily ~1.3-minute pass rather than a weekly one. Writes are idempotent upserts, so that is wasted
work, not corruption. It never deep-extracts.

Add `--no-session-narration` if "Hey Edith" stops responding while other Claude Code sessions are
active. The half-duplex mic gate skips wake detection entirely while EDITH is speaking (plus a
2.5s cooldown after), and session narration has no cooldown of its own — with several active
sessions she can talk almost continuously and never hear the wake word. The flag disables session
narration only; the underlying gate is unchanged, so wake still pauses while EDITH speaks for any
other reason. Narration is **on by default**.

### Always-on (launchd)

The daemon above dies with the terminal. To have it start at login and respawn on crash, see
[`deploy/README.md`](deploy/README.md) for the full runbook. Short version:

```bash
mkdir -p ~/.edith/logs && chmod 700 ~/.edith
chmod 600 .env          # the launcher REFUSES to boot without this — see below
sed -e "s#__EDITH_REPO_DIR__#$HOME/gitstuff/EDITH#g" \
    -e "s#__EDITH_HOME_DIR__#$HOME#g" \
    deploy/com.gsapify.edithd.plist > ~/Library/LaunchAgents/com.gsapify.edithd.plist
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.gsapify.edithd.plist
```

`launchctl bootout gui/$UID/com.gsapify.edithd` stops it — and that is the **only** way to free
the graph for the viewer, finder or ingest. A Control API `kill` is immediately undone by
`KeepAlive`, and `pause` does not release the Kuzu handle at all.

`edithd-launcher.sh` **sources** `.env`, which executes it. A group- or world-writable `.env` is
therefore arbitrary code running as you at every login, and a world-readable one leaks your
gateway key to any other local account. The launcher refuses to start unless `.env` is mode 600
and owned by you, so a permission regression fails loudly instead of silently re-exposing the key.

### Menu bar

A separate process that talks to the running daemon over its unix socket:

```bash
source .venv/bin/activate      # REQUIRED: uv installs into .venv, but a bare `python`
                               # on PATH is a different interpreter and won't see rumps
uv pip install -e '.[menubar]'
python -m edith.menubar        # --data-dir if the daemon uses a non-default one
```

If you see `[menubar] cannot start: the menu-bar app needs the 'rumps' package` right after
installing it, that is this exact mismatch — `which python` is not `.venv/bin/python`. Either
activate the venv or run `.venv/bin/python -m edith.menubar` directly.

Pause / resume / kill plus a status label. `edith/daemon/client.py` drives the same Control API
from the CLI if you prefer that.

### Individual subsystems

Handy for debugging one layer:

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
  guard/      pure policy: authorize / Decision + windowed token budget (one per daemon)
  daemon/     edithd composition root, Control API (unix socket), RuntimeState, SecureStore
  skills/     Skill contract + gh runner + PRReviewSkill + SessionQuerySkill
  desktop/    command parser + RepoResolver + osascript/open executors (DesktopControlSkill)
  voice/      TTS adapters (ElevenLabs/Piper) + VoiceIO + live wake/STT loop + persona
  session/    transcript collector + SessionBus + Narrator (session awareness)
  finder/     NL repo finder + resolve-on-miss
  ingest/     repo → graph pipeline (discover → fetch → redact → classify → map) + workspace pass
  viewer/     stdlib local graph viewer
  menubar/    rumps shell over the Control API (optional [menubar] extra)
```

## Development

```bash
pytest                    # 416 passed, 2 skipped
ruff check edith tests    # lint (scoped: scripts/sagemaker has 3 pre-existing SIM115)
pyright                   # types (basic mode; see the note below)
```

Test-first (red→green). Hardware/network behind injectable seams; live smokes are owner-run and
documented per slice. See each spec's Completion Record for the exact verification it shipped with.

Two baseline quirks worth knowing before you chase them: `ruff check .` reports **3 pre-existing
SIM115** in `scripts/sagemaker/train_hey_edith.py` (a training script, not library code), and
pyright emits `reportMissingImports` for `httpx`/`keyring`/`rumps` because it is not resolving
`.venv`. A third: plain `uv run pytest` fails to resolve on a machine with Python 3.14 installed
(`edith[voice]`'s `onnxruntime<1.20` pin vs `fastembed`'s marker split) — use `uv run --frozen`,
or `.venv/bin/python -m pytest`. None of the three is caused by your change.

## Known gaps

- **Guard's `authorize` decides nothing in the shipped config.** The budget half is real and
  metering. The authorization half is not: `Guard`'s default denylist and the desktop `Intent`
  vocabulary are disjoint sets, and `needs_confirmation` is a `False` class constant, so
  `authorize()` returns ALLOW for every OS action the daemon can take. The reachable exploit that
  came with that — `"open /tmp/evil.app"` running an attacker-planted bundle — is closed by
  refusing bundle *paths*, but the gate itself is inert. Making `Guard` default-deny over its
  closed enum is the real fix and is its own change.
- **No `SIGTERM` handler.** `launchctl bootout` is therefore an *ungraceful* stop:
  `EdithDaemon.stop()` never runs, so `compact()` and `MemoryStore.close()` are skipped. Wants a
  signal handler plus a `KeepAlive` policy that distinguishes an intentional stop from a crash.
- **EDITH ignores turns during a graph refresh.** The refresh reuses the daemon's Kuzu handle from
  a worker thread, and rather than take a cross-thread lock it folds into the existing `is_paused`
  predicate — so for ~1.3 minutes a live turn is skipped entirely: no reply, not even an apology.
  Deliberate (it avoids a whole class of deadlock for a once-a-week event), but worth knowing.
  `BackgroundReasoner.on_done` and `finder/resolve.py`'s deep-extract still write that handle
  without checking `is_paused`, so they can in principle race the refresh thread.
- **Logs are neither redacted nor rotated.** `sanitize_text` is the choke-point for model, TTS and
  bus payloads; it does not run on log records. Python's `lastResort` handler sends WARNING and
  above to stderr, which the LaunchAgent captures permanently. Keep `~/.edith` at mode 700.
- **Kuzu embedded is single-process.** The production fix is routing all DB access through
  `edithd`; today it is "only run one thing at a time", and an always-on daemon means `bootout`
  before the viewer, finder or ingest.
- **Owner live-smoke outstanding** for everything that needs real hardware or a real session:
  `launchctl bootstrap`, the menu bar rendering and its confirm dialog, the full voice loop,
  desktop-control OS actions (Spotify / Terminal / OMC), background reasoning's spoken ping, and
  a real graph-refresh pass. These paths are seam-tested, not hardware-verified.

## Where to look

| File | What it is |
|------|------------|
| [`docs/specs/00-north-star.md`](docs/specs/00-north-star.md) | Full architecture. **Read this first.** |
| [`docs/specs/01`–`13`](docs/specs/) | Per-slice specs + Completion Records (01–06 are the numbered slices; 07–13 the follow-ons) |
| [`deploy/README.md`](deploy/README.md) | launchd install/uninstall runbook + the Kuzu-lock consequences |
| [`docs/SESSION-PROTOCOL.md`](docs/SESSION-PROTOCOL.md) | How to resume across sessions (the 90%-context rule) |
| [`STATE.md`](STATE.md) | Current status + what to do next — **the resume file** |
| [`BUILD_LOG.md`](BUILD_LOG.md) | Running log of every build session |

## Resuming work

Read `docs/SESSION-PROTOCOL.md`, then `STATE.md`, then the relevant spec. Build test-first.
At session end (or ~90% context), append a Completion Record to the spec and update `STATE.md` +
`BUILD_LOG.md`, then stop.
