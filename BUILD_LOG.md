# EDITH — Build Log

Append-only, newest session at the bottom of each section. Every build session adds an entry.
This is the narrative history; `STATE.md` is the current snapshot.

---

## Session 1 — 2026-07-05 — Brainstorm → Spec Set

**Goal:** Convert the founding brainstorm into durable spec files so no context is lost across
future (token-limited) build sessions. No app code this session.

### Origin / motivation
Owner (Akhil Singh, AI Engineering Lead @ Pattern) wants an always-on personal AI presence.
Pain today: runs OMC + Claude Code across many terminals, but each session is stateless — he
re-explains context every time, and manually opens Slack/GitHub to find and review PRs. EDITH
removes that "typing + re-giving-context" layer.

### Vision (what "done" feels like)
- Ambient, voice-first. Only visible surface = a **menu-bar control**: pause / resume / kill.
  Everything else runs under the hood in a daemon (`edithd`).
- Knows his projects, working style, and current work without being told.
- Can be handed a fuzzy command ("EDITH, review Tavishi's PR") and it finds the channel, finds
  the PR, reviews it, and **asks when unsure** rather than guessing.
- Watches every running OMC / Claude Code terminal and can narrate what they're doing — e.g. he
  pastes an Airflow error into one terminal; EDITH observes and tells him what that session is
  doing about it.
- Voice control of the desktop ("open Spotify and play X"; launch a terminal, `cd` to a repo,
  start OMC).
- Feels like it never forgets and never needs a "new chat."

### Naming (LOCKED)
- System / repo: **EDITH** (Even Dead I'm The Hero). GitHub: `GSAPify/EDITH`.
- Daemon process: **edithd**.
- Voice: **Jarvis-style** (British male, MCU-Jarvis vibe) — but the *system* is EDITH, not Jarvis.
  Do NOT write "jarvis" as the system name anywhere in specs.
- Local repo path: `~/gitstuff/EDITH`.

### Decisions (LOCKED unless a spec-review gate reopens them)

1. **Model backend — Bifrost (Anthropic-compatible proxy).**
   Owner supplies base_url + API key with generous limits (Pattern's Bifrost gateway; note
   `brain-platform` / bifrost repos exist in `~/gitstuff`). Router picks haiku / sonnet / opus
   over the single endpoint. Provider-agnostic adapter so the backend can be swapped via `.env`.
   Cost is "covered by proxy limits" but NOT infinite → see cost governance below.

2. **Autonomy — "confirm risky, auto the rest."**
   AUTO (no prompt): read repos, review PRs, open apps, launch terminals, `cd`, narrate.
   ASK first: `git push`, PR merge, deletes, destructive shell, messaging people in his name,
   anything writing to shared/external state. Matches owner's CLAUDE.md guardrails
   (no direct push to main, confirm shared-state actions). Leash can loosen slice-by-slice as
   trust builds.

3. **Voice — pluggable TTS adapter, ElevenLabs primary, local fallback.**
   Owner wants ElevenLabs-level quality ("make it better like from elevenlabs"). Adapter so the
   engine is swappable (ElevenLabs streaming primary; local neural TTS — Piper/XTTS — as
   free/private fallback). Wake word + STT run **local**. FLAG: cloning the exact film Jarvis
   voice is legally gray — the adapter keeps the engine swappable so this stays a config choice,
   not load-bearing.

4. **Memory — graph DB core + vector recall, local-first.**
   Owner explicitly chose a real graph DB (relationship-heavy domain: project→repo→PR→person).
   Default to **Kuzu (embedded, no server)** to keep ops light per owner's "don't over-engineer"
   rule; owner is OPEN to a server (Neo4j) or another store "if that's what makes it best" — so
   the spec should pick the best-in-class option and justify it, with a server allowed where a
   slice genuinely needs one. Pair graph with a vector index for semantic recall. Retrieval +
   compaction (~50% context) = the "unlimited context" *feeling*.

### Cross-cutting requirements the spec MUST answer (raised in review)

- **Secrets boundary 🔒** — EDITH will read owner's CLAUDE.md, which contains LIVE OAuth tokens +
  client secrets (a real example of the risk). "Store everything" + persistent DB + Bifrost
  calls = creds could be persisted and sent over the wire. Spec must define: never-persist list,
  redact-before-model-call, secrets in macOS Keychain, DB encrypted at rest.
- **Cost / token governance** — an always-on daemon narrating every terminal is a textbook silent
  token-burner. Spec a budget + per-event gating (which events deserve a model call vs. handled
  locally), even with generous Bifrost limits.
- **Honest framing (no unicorns)** — "unlimited context" = memory + retrieval + compaction.
  "Two agents in one inference / haiku talks while opus thinks" = **orchestration of two calls**
  (fast model masks latency of the slow one), NOT a single inference. Say so in the specs.

### Reality-checks from the actual machine

- Repos live in `~/gitstuff/` (e.g. `~/gitstuff/concorde_lib`) — NOT `github/concord/lib`.
- **iTerm is NOT installed.** Available: Spotify, `say`, `osascript`, `ffmpeg`, `sqlite3`, `uv`,
  `node`, `bun`, `docker` (Rancher). `ollama` NOT installed.
- System Python is 3.9.6 — too old for modern voice/ML libs. Use **uv-managed Python 3.11+**.
- Session-awareness (Slice 4) taps OMC via hooks + `.omc/logs` / `.omc/state/sessions/{id}/` —
  this is an ASSUMPTION, spec it as "to prototype / verify event source first."

### Architecture (summary — full version in `docs/specs/00-north-star.md`)
```
menu bar: EDITH [pause][kill]  ← only visible surface
   │ controls (unix socket / localhost)
   ▼
edithd (native, uv py3.11+)
  VOICE ─► BRAIN/ORCHESTRATOR ─► ROUTER ─► Bifrost (haiku/sonnet/opus)
              ├─ MEMORY (graph + vector, local, encrypted)
              ├─ SESSION BUS (watch OMC/CC terminals)
              ├─ SKILLS (PR review, Airflow, Slack, desktop)
              └─ GUARD (redaction + autonomy gate + budget)
```
Docker for stateful backend if needed; voice + desktop-control + menu-bar MUST be native (mic,
osascript, app launching can't run in a container).

### Build order (each slice ships something usable before the next starts)
1 Memory+Brain · 2 PR-review · 3 Voice · 4 Session-awareness · 5 Router · 6 Desktop-control.
Spec depth this session: DEEP on 0 (north-star) + 1 (memory/brain); INTERFACE-level on 2–6.

### Session 1 work log
- [x] Created `~/gitstuff/EDITH`, git init, wired remote `GSAPify/EDITH`.
- [x] Scaffolding: `.gitignore`, `README.md`, `STATE.md`, `BUILD_LOG.md`.
- [x] Wave 1 (opus agent): `00-north-star.md` + `SESSION-PROTOCOL.md` + spec `_TEMPLATE.md`.
- [x] Wave 2 (6 parallel agents): `01`–`06` slice specs. Slice 1 opus/deep (555 ln), 2–6 sonnet.
- [x] Reconciled: no terminology drift, no unicorn claims, deferred decisions resolved.
- [x] Commit + push (branch `spec/session-1-foundation`).

### Session 1 outcome — SPEC SET COMPLETE
Full spec set authored (~2950 lines across 8 docs). Notable decisions the slice agents resolved
during authoring:
- **Slice 1 storage** — Kuzu **native HNSW vector index** as primary (verified against current
  Kuzu docs via context7), `sqlite-vec` as documented fallback. Resolves north-star OQ#1.
- **Slice 6 terminal driver** — hybrid: **spawn-and-own a shell process** for OMC/Claude Code
  launches (reliable, stdout capture, knows when `cd` finished); **Terminal.app via `osascript`**
  for the "open a terminal I can watch" path. Resolves north-star OQ#2. (iTerm still not required.)
- **Slice 4** — spec mandates a **prototype/spike to verify the OMC/Claude Code event source**
  before building the SessionBus interface (highest-uncertainty piece).

**Method note:** parallel-agent authoring worked well; agents wrote to disk and returned summaries
only (budget discipline). A single giant Write in the main thread timed out earlier — lesson:
chunk writes / delegate authoring. Commit-early-and-often saved the session after that timeout.

**Next session (Session 2):** build **Slice 1 (Memory + Brain)**. Needs Bifrost base_url + key.

<!-- Next sessions append below this line -->
