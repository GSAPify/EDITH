# 06 — Desktop Control

> **Honest-framing reminder:** no unicorns. "Unlimited context" = memory + retrieval +
> compaction; "two agents in one inference" = orchestration of two model calls (fast masks
> slow). If a section here implies a capability that doesn't exist, fix the section.
>
> This slice follows the shape below. Architecture-level interfaces + cross-cutting rules are
> fixed in `00-north-star.md` — **do not restate them, reference them.** This file adds
> *implementation* depth for this slice only.

## Terminology (glossary)

| Term | Meaning |
|------|---------|
| **EDITH** | The system. Always-on local-first macOS assistant. |
| **edithd** | The daemon process that runs everything under the hood. |
| **bus** | In-process event/message bus; components `publish`/`subscribe`. |
| **Guard** | Cross-cutting enforcement: `redact`, `authorize` (allow/ask/deny), budget. |
| **Router** | `model_call(messages, tier_hint) -> response` over the Bifrost adapter. |
| **Memory** | Graph + vector store: `recall` / `remember` / `compact`. |
| **Skill** | Capability with `name`, `triggers`, `needs_confirmation`, `run(context)->result`. |
| **tier** | Model size class the Router selects: haiku / sonnet / opus. |
| **repo-map** | Memory-backed table of fuzzy repo name → absolute path under `~/gitstuff/`. |

---

## Purpose

Slice 6 delivers voice-driven macOS automation. It ships as one or more Skills (`run(context)`)
that translate a voice utterance into concrete OS actions: launching applications, controlling
Spotify playback, opening a terminal session, `cd`-ing to a repo, and starting OMC / Claude
Code inside it. The owner can say "open Spotify and play Concorde" or "launch a terminal in the
concorde_lib repo and start OMC" and the action executes without touching the keyboard.

---

## Scope

**In:**
- Launching macOS applications via `open -a`
- Spotify playback control (play, pause, skip, set volume) via AppleScript
- Opening a terminal session (Terminal.app), `cd`-ing to a resolved repo path, and optionally
  running `claude` (OMC entry-point) inside it
- Fuzzy repo name → path resolution backed by Memory
- Autonomy gate enforcement: AUTO for open/launch/cd/play; ASK for anything destructive or
  writing shared state

**Out:**
- Arbitrary shell script execution (too broad — not in v1)
- Driving apps other than Spotify and Terminal.app via AppleScript (deferred to later slices
  or explicit extension points)
- Cross-machine / remote desktop control
- GUI interaction beyond AppleScript (no accessibility-API scraping in v1)

---

## Command → Action Flow

```
Voice utterance
      │
      ▼
┌─────────────────────────────────┐
│  BRAIN / ORCHESTRATOR           │
│  Receives voice.utterance event │
│  Classifies → desktop intent    │
└────────────┬────────────────────┘
             │  run(context)
             ▼
┌─────────────────────────────────────────────────────────┐
│  DESKTOP-CONTROL SKILL                                   │
│                                                          │
│  ┌──────────────────┐    ┌──────────────────────────┐   │
│  │  CommandParser   │    │  RepoResolver             │   │
│  │  (haiku call or  │    │  fuzzy name → ~/gitstuff/ │   │
│  │   regex fast-    │    │  path via Memory          │   │
│  │   path)          │    └───────────┬──────────────┘   │
│  └────────┬─────────┘                │                   │
│           │ parsed action            │ resolved path     │
│           ▼                          ▼                   │
│  ┌────────────────────────────────────────────────┐      │
│  │  Guard.authorize(action) → allow | ask | deny  │      │
│  └────────────────────┬───────────────────────────┘      │
│                       │ allow                            │
│           ┌───────────┼───────────────┐                  │
│           ▼           ▼               ▼                  │
│    ┌────────────┐ ┌──────────┐ ┌──────────────────┐      │
│    │ open -a    │ │osascript │ │ Terminal launcher │      │
│    │ (app open) │ │(Spotify) │ │ (osascript +      │      │
│    └────────────┘ └──────────┘ │  do script)       │      │
│                                └──────────────────┘      │
└─────────────────────────────────────────────────────────┘
             │
             ▼
   publish(skill.result, { action, status, spoken_summary })
             │
             ▼
   VoiceIO.speak("Opening Spotify…")
```

---

## Interface to edithd

- **Inputs:**
  - `voice.utterance` events from BRAIN when the intent is classified as desktop action
  - `run(context)` call where `context.intent` = `open_app | spotify_control | terminal_launch`
    and `context.raw_utterance` carries the original text

- **Outputs:**
  - `skill.result` event on the bus: `{ skill: "desktop-control", action, status: ok|error, spoken_summary }`
  - `speak(text)` via VoiceIO for spoken confirmation / error feedback

- **Bus events:**

  | Topic | Direction | Payload |
  |-------|-----------|---------|
  | `voice.utterance` | subscribed | `{ text, ts }` |
  | `skill.result` | published | `{ skill, action, status, spoken_summary, ts }` |

- **Control contracts:** none — this slice does not add new Control API surface. It
  respects the existing `pause` command (no OS actions fired while paused).

---

## Data model

**repo-map** (stored in Memory graph, not a separate DB):

```
Node: Repo
  name:        str   # canonical name, e.g. "concorde_lib"
  aliases:     list  # fuzzy variants, e.g. ["concorde", "concordelib"]
  abs_path:    str   # ~/gitstuff/concorde_lib
  last_used:   ts
```

Edges: `Project -[HAS_REPO]-> Repo` (reuses the project→repo relationship from Slice 1's
graph schema). No new graph nodes needed beyond `Repo`.

Known repos are seeded at first run by scanning `~/gitstuff/` for directories that contain a
`.git` folder. Memory stores them; new repos discovered later are added on first successful
resolution.

---

## Dependencies

- **Other slices:**
  - **Slice 1 (Memory + Brain)** — repo-map lives in the Memory graph; `remember`/`recall`
    used for alias lookup. BRAIN classifies the intent and calls `run(context)`.
  - **Slice 3 (Voice)** — `voice.utterance` is the trigger; `speak()` delivers spoken feedback.
  - **Slice 5 (Router)** — used only when fast-path regex misses and CommandParser falls back
    to a haiku model call for intent extraction.

- **Libraries:**
  - `subprocess` (stdlib) — `open -a`, `osascript` invocation
  - `pathlib` (stdlib) — path validation, `~/gitstuff/` scanning
  - `re` (stdlib) — fast-path regex before any model call
  - No new third-party libraries required for the core mechanism

---

## Tech choices

### Terminal driver — RECOMMENDATION: spawn and own a shell process (v1)

**The three options considered:**

| Option | Mechanism | Pro | Con |
|--------|-----------|-----|-----|
| A. Drive Terminal.app via `osascript` | `do script` in a new window | No install required | Window management is fragile; `do script` returns immediately, no way to know when `cd` finished; no stdout capture |
| B. Install iTerm2 or Ghostty | Rich AppleScript / RPC API (iTerm2) | Reliable scripting, rich control | Requires owner to install a tool; iTerm2's AppleScript API is sprawling and version-sensitive |
| C. Spawn a managed shell process (`subprocess.Popen`) | EDITH owns the shell's stdin/stdout | Full control over cwd, env, when commands finish; no install; stdout capture for future narration; no window-management fragility | No visible terminal window (daemon-side shell, headless) — owner must launch a visible terminal separately if they want to watch |

**Recommendation: Option C (spawn owned shell) for the OMC-launch path; Option A (Terminal.app `osascript`) for the "open a terminal I can see" path.**

Rationale:
- The primary OMC-launch use-case ("launch a terminal in concorde_lib and start OMC") does not
  require a *visible* window — OMC runs happily headless inside a daemon-owned shell. EDITH
  already watches sessions via SessionBus (Slice 4), so stdout narration works without a visible
  window.
- If the owner says "open a terminal in concorde_lib" without asking to start OMC, Option A
  (`osascript` → `do script "cd ~/gitstuff/concorde_lib"`) opens a visible Terminal.app window
  with minimal fragility — acceptable for an interactive use-case where the owner is watching.
- This avoids a required install while keeping the automation-critical OMC path reliable and
  observable.

**Tradeoff the owner should know:** The daemon-owned shell is invisible by default. If the owner
wants to watch what OMC is doing in a window, Slice 4 (SessionBus) already narrates it via
voice. A future extension could open a Terminal.app window *and* attach a pty to the same
process (iTerm2 makes this trivial; Terminal.app does not). If the owner installs Ghostty or
iTerm2 later, swap in the scripted-window path behind the same `run(context)` interface with no
other changes.

---

### Spotify control — osascript AppleScript API

Spotify exposes a stable AppleScript dictionary on macOS. All playback commands go through
`osascript -e '...'`:

```
play track:    tell application "Spotify" to play track "spotify:search:<query>"
pause:         tell application "Spotify" to pause
next:          tell application "Spotify" to next track
set volume:    tell application "Spotify" to set sound volume to <0-100>
current track: tell application "Spotify" to get name of current track
```

Song search uses the `spotify:search:` URI scheme — Spotify resolves it to the best match.
No Spotify Web API credentials needed for playback from the desktop app.

### App launching — `open -a`

```bash
open -a "Slack"
open -a "Notion"
open -a "Spotify"
```

App names are matched case-insensitively. If `open -a` exits non-zero (app not found), the
Skill reports the error via `skill.result` and speaks a correction.

### Command parsing — regex fast-path, haiku fallback

Intent classification in order:
1. **Regex fast-path** — patterns for the highest-frequency commands (Spotify play/pause/skip,
   `open <app>`, `terminal in <repo>`, `start OMC in <repo>`). Zero model calls, sub-1ms.
2. **Haiku fallback** — only when no regex matches. Sends the utterance to Router with
   `tier_hint=haiku`. Returns a structured JSON action dict `{ intent, app?, repo?, query? }`.
   Single call, cheap.

---

## Repo resolution

```
fuzzy_name  ──► Memory.recall("repo aliases: <fuzzy_name>")
                    │
                    ├── hit  → abs_path returned directly
                    │
                    └── miss → scan ~/gitstuff/ for best substring/fuzzy match
                                    │
                                    ├── unambiguous → remember alias, return path
                                    └── ambiguous   → ASK owner to clarify
```

Fuzzy matching uses simple substring containment and Levenshtein distance (stdlib
`difflib.get_close_matches`) — no ML, no model call for resolution itself.

Known path corrections (per north-star §8):
- Repos live under `~/gitstuff/` — NOT `~/github/`, NOT `~/concord/lib/`, NOT any other prefix.
- `concorde_lib` resolves to `~/gitstuff/concorde_lib`.

---

## Autonomy & secrets notes

**Autonomy gate** (per north-star §6.3):

| Action | Gate |
|--------|------|
| `open -a <app>` | AUTO |
| Spotify play / pause / skip / volume | AUTO |
| `cd` to a repo in owned shell | AUTO |
| Start `claude` (OMC) in owned shell | AUTO |
| Open Terminal.app window (`do script`) | AUTO |
| Any shell command beyond `cd` and `claude` | ASK |
| `rm`, `git push`, destructive shell | ASK (always) |
| Running an arbitrary user-supplied shell string | DENY in v1 |

The Skill declares `needs_confirmation: False` for the AUTO set and `needs_confirmation: True`
for anything beyond it. Guard enforces this; the Skill does not bypass Guard.

**Secrets:**
- This slice does not handle credentials directly.
- The repo scan reads directory names only — no file contents, no `.env` reads.
- The `osascript` subprocess receives no secrets in its arguments.
- `spoken_summary` published to the bus (and potentially the model) contains only action
  metadata (app name, repo name, command type) — never file contents or credential values.
  Guard.redact runs on all bus payloads before any model call, per north-star §6.1.

---

## Cost / token notes

The vast majority of desktop-control actions cost **zero model calls**:

- Regex fast-path handles common patterns (Spotify, `open`, terminal launch) with no inference.
- Repo resolution uses Memory lookup + `difflib` — local, no model.
- `osascript` / `subprocess` execution — local.

A haiku call fires only when the regex fast-path fails to classify the utterance. Expected
frequency: rare (novel phrasings on first encounter; cached/regex thereafter). Per north-star
§6.2, Guard gates model calls; this slice earns a haiku call only when intent is genuinely
ambiguous.

Spoken confirmation (`speak()`) uses TTS (ElevenLabs or local Piper) — not a model call.

Budget impact: negligible. This is the cheapest slice in the system.

---

## Build steps (high-level, ordered)

1. **Scaffold the Skill module** — `src/skills/desktop_control.py` implementing the Skill
   contract: `name`, `triggers`, `needs_confirmation`, `run(context) -> result`.

2. **Repo scanner** — on first run, walk `~/gitstuff/` for `.git` dirs and `remember` each as
   a `Repo` node with canonical name + abs_path. Wire into Memory (requires Slice 1).

3. **CommandParser** — regex fast-path table for top-5 command types; haiku fallback for
   misses. Return a typed `DesktopAction` dataclass.

4. **RepoResolver** — `resolve(fuzzy_name) -> Path | AmbiguousError`. Memory lookup first,
   then `difflib.get_close_matches` over `~/gitstuff/`, then ASK.

5. **Executor functions** — four thin functions, each a subprocess/osascript call:
   - `launch_app(name: str) -> None` — `open -a`
   - `spotify_command(cmd: SpotifyCmd, query: str | None) -> None` — `osascript`
   - `open_terminal_window(path: Path) -> None` — `osascript do script`
   - `spawn_shell_session(path: Path, run_cmd: str | None) -> subprocess.Popen` — owned shell

6. **Autonomy gate wiring** — each executor function calls `Guard.authorize(action)` before
   running. No executor bypasses Guard.

7. **Bus wiring** — subscribe `voice.utterance` in BRAIN's router; publish `skill.result`
   after execution; call `VoiceIO.speak()` with the spoken summary.

8. **Smoke tests** — unit tests for CommandParser (regex coverage + haiku mock), RepoResolver
   (hit / miss / ambiguous), and each executor (subprocess mock). Integration test: full
   `run(context)` with a mocked osascript call.

---

## Verification / testing

**Unit (no OS side-effects):**
```bash
uv run pytest tests/skills/test_desktop_control.py -v
```
- CommandParser: at least 10 utterance fixtures — 8 regex hits, 2 haiku fallbacks (mocked).
- RepoResolver: seed a temp `~/gitstuff/` tree; assert exact match, alias match, ambiguous
  raises, miss raises.
- Executor functions: mock `subprocess.run` / `subprocess.Popen`; assert correct
  `osascript` argument strings are constructed.
- Guard gate: assert that any action in the ASK set does not call the executor without
  confirmation.

**Manual smoke test (requires running daemon):**
```
"EDITH, open Spotify"
  → Spotify launches (or focuses if already open)
  → EDITH speaks: "Opening Spotify."

"EDITH, play Bohemian Rhapsody on Spotify"
  → osascript fires play track "spotify:search:Bohemian Rhapsody"
  → EDITH speaks: "Playing Bohemian Rhapsody."

"EDITH, start OMC in concorde_lib"
  → RepoResolver returns ~/gitstuff/concorde_lib
  → spawn_shell_session(path, run_cmd="claude") fires
  → EDITH speaks: "Starting OMC in concorde_lib."
  → Slice 4 SessionBus begins tracking the new session

"EDITH, open a terminal in concorde_lib"
  → open_terminal_window(~/gitstuff/concorde_lib) fires
  → Terminal.app window opens at that path
  → EDITH speaks: "Terminal opened in concorde_lib."
```

**Autonomy gate manual test:**
- Issue a command that should hit ASK (e.g., a freeform shell command).
- Confirm EDITH asks before executing and does not proceed without owner confirmation.

---

## Open questions

- **`claude` binary location** — the OMC entry-point is `claude` (Claude Code CLI). Its
  location in the uv-managed environment needs to be verified at build time (likely
  `~/.local/bin/claude` or on PATH). Owner to confirm or `which claude` at build session start.

- **Spotify search URI quality** — `spotify:search:<query>` picks Spotify's top result, which
  may not match owner intent for ambiguous queries (e.g. "play Concorde" could match band or
  song). A v1.1 extension could call the Spotify Web API for a ranked search with spoken
  disambiguation ("Did you mean Concorde by X or Y?"). For v1, `spotify:search:` is accepted as
  good-enough.

- **Terminal.app `do script` working directory** — `do script "cd ~/gitstuff/foo"` opens a
  window but the `cd` executes after the default shell profile loads; on some shell
  configurations the working directory may not persist visually. Test at build time. If it
  behaves inconsistently, switch to `do script "exec zsh -c 'cd ~/gitstuff/foo && exec zsh'"`.

- **Owned shell TTY** — `subprocess.Popen` for the OMC session is headless. If the owner
  wants a visible window for the OMC session, a pty pair (`pty.openpty`) + Terminal.app window
  attach is needed. This is a v1.1 concern; Slice 4 SessionBus provides narration in the
  interim.

---

## Completion Record — Desktop Control — 2026-07-12 (Session 16)

- **What shipped:** Voice-driven macOS automation as one Skill. The owner can say
  "open Spotify", "play Bohemian Rhapsody on Spotify", "pause the music", "set the volume
  to 40", "open a terminal in concorde_lib", or "start OMC in concorde_lib" and the action
  fires — no keyboard. New `edith/desktop/` package (parser + resolver + executors) and
  `DesktopControlSkill`, registered in `edithd`'s Brain LAST.

- **How it works:** `parse_command` (regex fast-path, zero model calls) classifies the
  utterance into a typed `DesktopAction`; a miss falls back to a single haiku classify ONLY
  when a Router is wired. `RepoResolver` maps a fuzzy repo name to an absolute path by
  scanning `~/gitstuff/` two levels deep (flat repos + org-nested clones) and fuzzy-matching
  with `difflib` — model-free. Three executor functions (`launch_app`, `spotify_command`,
  `open_terminal`) each build an argv / AppleScript string and hand it to an injected
  `Runner` seam (default = `asyncio.create_subprocess_exec`; tests inject a recorder). The
  Skill: parse → (resolve repo for terminal/OMC) → execute via the seam → `speak` a summary.

- **Key decisions made during build:**
  - **Terminal.app `do script` for BOTH the visible-terminal AND the OMC-launch path** (see
    Deviations). One parameterized `open_terminal(path, run_cmd=None)`; `run_cmd="claude"`
    for OMC. No `Popen` lifecycle in the daemon.
  - **`RepoResolver` prefers the flat `~/gitstuff/<name>` over a nested `~/gitstuff/<org>/<name>`
    of the same basename.** The bulk workspace pull (`clone_workspace.sh`) clones into
    `~/gitstuff/<org>/`, so a repo the owner also works on flat exists in BOTH places with an
    identical remote (verified live: `~/gitstuff/agents` and `~/gitstuff/patterninc/agents`
    both → `github.com/patterninc/agents.git`). Preferring the shallow working copy is picking
    one repo's working copy, not guessing between two. Genuine ambiguity = two copies at the
    SAME depth (e.g. `patterninc/x` + `ampmedia/x`, no flat tiebreaker) → still `AmbiguousRepo`
    → ASK. The depth-preference lives ONLY in `_one` (the identical-basename bucket); the
    cross-name fuzzy path still raises on multiple distinct matches.
  - **`needs_confirmation = False`, no ASK/DENY code paths.** Every action the parser emits is
    in the spec's AUTO set; the parser never produces a destructive/freeform-shell action, so
    there is no reachable confirm branch to build (YAGNI).

- **Deviations from spec + why:**
  - **OMC launch via Terminal.app, not the spec's Option C headless owned shell.** `claude`
    (the OMC entry-point) is an interactive TTY REPL and will not run under a pipe-backed
    `Popen` — the spec flagged this in Open Question #4. Terminal.app `do script` gives a real
    TTY and a visible window; Slice 4 SessionBus still narrates the session by tailing its
    transcript. Recommended in advisor review.
  - **No new graph schema.** The spec's `repo-map` data model proposed `Repo.aliases` /
    `abs_path` / `last_used` columns. Sanctioned by the spec's honest-framing reminder: the
    live graph's `Repo` nodes are metadata-only (empty `path`), so a schema migration would buy
    nothing. Resolution is filesystem-first + `difflib`; Memory is not consulted for paths in v1.
  - **Prefer-flat resolution** (above) is a refinement over the spec's plain hit/ambiguous split.

- **Files created / changed:**
  - `edith/desktop/__init__.py`, `edith/desktop/control.py`, `edith/desktop/executors.py` (new)
  - `edith/skills/desktop_control.py` (new); `edith/skills/__init__.py` (export)
  - `edith/daemon/edithd.py` (register `DesktopControlSkill` last, ~10 lines)
  - `tests/test_desktop_control.py` (new, 30 cases)

- **Verification / tests run + results:** Full suite **258 passed, 2 skipped**; ruff clean;
  pyright clean on all Slice-6 files (repo has 17 pre-existing pyright errors on master,
  untouched — out of scope). **Safe live checks (no side effects):** parsed all 6 spec
  canonical utterances correctly; `osacompile`d the generated Spotify + Terminal AppleScript
  (compiles clean, including an embedded-quote case) — proves the escaping is well-formed
  without executing; ran `RepoResolver` against the real `~/gitstuff` (1400+ repos across flat
  + patterninc/ + ampmedia/) → `agents`/`agentsmith` resolve to the flat working copy, a
  bogus name returns clean NOT_FOUND.

- **Follow-ups / known gaps:**
  - **Owner LIVE-SMOKE still required** for the actual OS behaviour (Spotify opens, Terminal
    launches, OMC starts). This project's recurring bite: tests green ≠ works live. Not run
    this session (intrusive during a build).
  - **Prefer-flat residual:** if a flat personal experiment shares a name with an org repo you
    meant, it picks the experiment. Low-probability for this layout, recoverable (wrong dir
    opens, correct it), acceptable v1.
  - **Broad triggers:** `"open "`/`"play "` are broad; if the parser can't classify a matched
    utterance the Skill speaks "I didn't catch that" and consumes the turn (Brain short-circuits
    on any trigger match). Mitigated by registering desktop LAST; a real query rarely leads with
    these verbs. Revisit if it steals real queries in practice.
  - **Haiku fallback** fires only when a Router is wired; JSON-classify is minimal (no retry).
  - **Guard** is still the deferred allow-by-default seam project-wide; this slice relies on the
    parser's AUTO-only output rather than a real authorize gate.
