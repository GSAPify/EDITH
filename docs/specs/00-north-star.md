# 00 — North Star (Authoritative Architecture)

> **Read this first.** This is the authoritative architecture for EDITH. It defines
> **interfaces + sequencing + cross-cutting rules** for all 6 slices. Per-slice
> *implementation* detail lives in `01-` … `06-` (authored separately). When those
> disagree with this file on an interface or a cross-cutting rule, **this file wins**;
> when they add implementation depth within their slice, they win there.
>
> Source of truth for every decision below: `BUILD_LOG.md` → Session 1. Do not
> contradict it.

---

## 1. Vision & Non-Goals

EDITH is an **always-on, local-first personal AI presence for macOS**. It is ambient and
voice-first. The only visible surface is a menu-bar control (pause / resume / kill);
everything else runs under the hood in the `edithd` daemon.

**What "done" feels like**
- Knows the owner's projects, working style, and current work without being told.
- Takes a fuzzy command ("review Tavishi's PR"), finds the channel, finds the PR, reviews
  it, and **asks when unsure** instead of guessing.
- Watches every running OMC / Claude Code terminal and can narrate what they're doing.
- Voice-controls the desktop (open apps, launch a terminal, `cd` to a repo, start OMC).
- Feels like it never forgets and never needs a "new chat."

**Non-goals (v1)**
- Not a chat window / not a GUI app. Menu-bar + voice only.
- Not multi-user, not cloud-hosted. One machine, one owner, local-first.
- Not a general web agent. Scope = the owner's dev workflow (repos, PRs, Slack, desktop).
- Not a model trainer. EDITH orchestrates existing models via Bifrost; it fine-tunes nothing.

### Honest framing (no unicorns) 🚫🦄

Two phrases from the brainstorm are marketing shorthand. The spec states plainly what they
actually are:

| Shorthand | What it actually is |
|-----------|---------------------|
| **"unlimited context"** | Persistent **memory + retrieval + compaction**. EDITH stores facts/edges in a graph, recalls the relevant slice per query, and compacts working context (~50%) so long-running sessions don't blow the window. There is no infinite window; there is durable recall that *feels* like one. |
| **"two agents in one inference" / "haiku talks while opus thinks"** | **Orchestration of TWO model calls.** A fast model (haiku) emits an immediate acknowledgement/filler while a slow model (opus) computes the real answer. The fast call **masks the latency** of the slow one. This is NOT one inference producing two personalities — it is two sequenced/overlapped calls behind the Router. |

Everything in this spec is buildable with the named libraries. No component depends on a
capability that does not exist today.

---

## 2. Naming (LOCKED)

- **System / repo:** `EDITH` (Even Dead I'm The Hero). GitHub: `GSAPify/EDITH`.
- **Daemon process:** `edithd`.
- **Voice:** **Jarvis-*style*** (British male, MCU-Jarvis vibe) — the *style* of the voice
  only. The system is **EDITH**. Never write "jarvis" as the system name anywhere.
- **Local repo path:** `~/gitstuff/EDITH`.

---

## 3. Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│  MENU-BAR CONTROL APP  (rumps, native)   ← only visible surface        │
│     EDITH  ▸ pause   ▸ resume   ▸ kill   ▸ status                       │
└───────────────┬────────────────────────────────────────────────────────┘
                │  Control API  (local unix socket, JSON lines)
                ▼
┌──────────────────────────────────────────────────────────────────────┐
│  edithd  (native, uv-managed Python 3.11+, launchd-supervised)         │
│                                                                        │
│   ┌────────┐   wake/utterance   ┌───────────────────────┐             │
│   │ VOICE  │ ─────────────────► │  BRAIN / ORCHESTRATOR  │             │
│   │  IO    │ ◄───── speak() ─── │  (plans, decides,      │             │
│   └────────┘                    │   sequences calls)     │             │
│                                 └───┬─────────┬──────┬────┘             │
│                                     │         │      │                 │
│                    model_call ┌─────▼──┐  ┌───▼───┐  │  run(context)   │
│                               │ ROUTER │  │ GUARD │  │ ┌────────────┐  │
│                               └───┬────┘  └───┬───┘  └►│  SKILLS    │  │
│                                   │           │        │ pr-review, │  │
│                                   ▼           ▼        │ airflow,   │  │
│                             Bifrost adapter  redact/   │ slack,     │  │
│                             (haiku/sonnet/   authorize/│ desktop    │  │
│                              opus)           budget    └────────────┘  │
│                                                                        │
│   ┌──────────────────────  INTERNAL EVENT / MESSAGE BUS  ────────────┐ │
│   │  every component publishes & subscribes here                     │ │
│   └───┬───────────────────────────────────────────────────┬──────────┘ │
│       │                                                    │            │
│   ┌───▼────┐                                        ┌──────▼──────┐     │
│   │ MEMORY │  recall / remember / compact           │ SESSION BUS │     │
│   │ graph+ │  (Kuzu + vector, encrypted at rest)    │ watch OMC/  │     │
│   │ vector │                                        │ CC terminals│     │
│   └────────┘                                        └─────────────┘     │
└──────────────────────────────────────────────────────────────────────┘
```

**Component responsibilities**

| Component | Owns |
|-----------|------|
| **Menu-bar control app** | The only UI. Talks to `edithd` over the Control API. Surfaces status; issues pause/resume/kill. Native (`rumps`), not in `edithd`. |
| **`edithd` daemon core** | Process lifecycle, the internal bus, wiring of all subsystems, launchd supervision, Control API server. The "spine." |
| **VOICE (VoiceIO)** | Local wake-word + STT in; TTS out. Emits `wake`/`utterance` events; provides `speak(text)`. |
| **BRAIN / ORCHESTRATOR** | The decision loop. Consumes bus events, plans, decides which skill/model to invoke, sequences the two-call latency-masking pattern, asks the owner when unsure. |
| **ROUTER** | Picks the model tier (haiku/sonnet/opus) and calls Bifrost through a provider-agnostic adapter. Owns the two-call pattern mechanics. |
| **MEMORY** | Durable graph (project→repo→PR→person) + vector recall. `recall`/`remember`/`compact`. Encrypted at rest. |
| **SESSION BUS** | Watches running OMC / Claude Code terminals, turns their events into session state on the internal bus. (Distinct from the internal bus — this is a *producer* of session events.) |
| **SKILLS** | Discrete capabilities (PR review, Airflow, Slack, desktop control). Uniform `run(context)` contract; declare triggers + confirmation needs. |
| **GUARD** | Cross-cutting enforcement: redaction before any model call, the autonomy gate (allow/ask/deny), and budget/token checks. |

---

## 4. Internal Interfaces

Two backbones connect everything: the **internal event/message bus** (component ↔ component,
in-process) and the **Control API** (menu-bar app ↔ `edithd`, cross-process).

### 4.1 Internal event/message bus

In-process pub/sub. Components never call each other directly for events; they publish and
subscribe. Keeps subsystems decoupled and makes SESSION-BUS/VOICE producers swappable.

```
publish(topic, event)                 subscribe(topic, handler)
        │                                     ▲
        ▼                                     │
   ┌─────────────────── BUS ───────────────────┐
   │  topics: voice.wake  voice.utterance       │
   │          session.event  session.state      │
   │          brain.decision  skill.result      │
   │          guard.blocked   budget.warning     │
   └────────────────────────────────────────────┘
```

- **Transport:** in-process async pub/sub within the single `edithd` process (asyncio
  queues or a thin event-emitter). Not a network broker — no Redis/Kafka. If a slice ever
  needs cross-process fan-out, wrap it behind the same `publish`/`subscribe` surface.
- **Event envelope:** `{ topic, ts, source, payload }`. Payloads are already
  Guard-redacted before they carry anything owner-sensitive.

### 4.2 Control API (menu-bar ↔ edithd)

Local **unix domain socket**, JSON-lines request/response. Localhost HTTP is the fallback if
a future control client can't speak unix sockets. Loopback/socket only — never a public bind.

| Command | Effect |
|---------|--------|
| `pause` | Halt autonomous action + model calls; VOICE keeps listening for `resume`/`kill`. |
| `resume` | Return to normal operation. |
| `kill` | Graceful shutdown of `edithd`. |
| `status` | Return `{ state, active_skill, budget_used, last_event }` for the menu-bar label. |

### 4.3 Component contracts (one line each)

| Component | Contract |
|-----------|----------|
| **Router** | `model_call(messages, tier_hint) -> response` — selects haiku/sonnet/opus over the Bifrost adapter; supports the two-call latency-masking pattern (fast ack + slow answer). |
| **Guard** | `redact(payload) -> safe`; `authorize(action) -> allow \| ask \| deny`; plus a budget check gating whether an event earns a model call. |
| **Memory** | `recall(query) -> context`; `remember(facts \| edges)`; `compact() -> ()` (shrinks working context ~50%). |
| **VoiceIO** | emits `voice.wake` / `voice.utterance` events; provides `speak(text) -> ()`. |
| **SessionBus** | ingests OMC / Claude Code events → publishes `session.event` / `session.state` on the internal bus. |
| **Skill** | declares `name`, `triggers`, `needs_confirmation`; provides `run(context) -> result`. |

---

## 5. Tech Stack (concrete, justified, light)

> Rule of the house (owner's CLAUDE.md): don't over-engineer. Pick best-in-class, justify
> it, add nothing speculative.

| Area | Choice | Why |
|------|--------|-----|
| **Runtime** | **uv-managed Python 3.11+** | System Python is 3.9.6 — too old for modern voice/ML libs. `uv` is already installed. |
| **Menu-bar app** | **`rumps`** | Minimal native macOS status-bar apps in Python. No Electron. |
| **IPC (Control API)** | **unix domain socket** | Local, fast, no port to secure; loopback HTTP fallback. |
| **Memory — graph** | **Kuzu (embedded, no server)** | Real graph DB for the relationship-heavy domain (project→repo→PR→person) with zero ops. Owner is **open to a server (Neo4j)** if a slice genuinely needs one — allowed, not default. |
| **Memory — vector** | **Kuzu-native vector index if mature**, else an embedded store (**LanceDB** or **sqlite-vec**) | Semantic recall to pair with graph traversal. Keep it embedded to match "no server" default. Final pick is a Slice-1 decision (see Open Questions). |
| **Secrets** | **macOS Keychain via `keyring`** | Never in files/DB. See §6. |
| **DB at rest** | **encrypted** | Memory holds owner-sensitive context; encrypt the on-disk store. |
| **Always-on** | **`launchd`** | Native supervision/restart; the daemon lives here, not a container. |
| **Model backend** | **Bifrost** (Anthropic/OpenAI-compatible) behind a **provider-agnostic adapter** | Pattern's gateway (base_url + key via `.env`). Swappable backend; Router picks the tier. |
| **STT + wake word** | **local** — `faster-whisper` (STT) + `openWakeWord` (wake) | Privacy + latency; no cloud round-trip to start listening. |
| **TTS** | **pluggable adapter** — **ElevenLabs primary**, local **Piper / XTTS** fallback | Owner wants ElevenLabs-level quality; adapter keeps engine a config choice (also the fix for the legally-gray voice-cloning concern — never load-bearing). |
| **Docker** | only for a **stateful backend if one is needed** (e.g. Neo4j) | Voice + desktop-control + menu-bar **must be native** — mic, `osascript`, app launching can't run in a container. `docker` (Rancher) is present. |

---

## 6. Cross-Cutting Rules (non-negotiable)

### 6.1 Secrets boundary 🔒

EDITH will **read the owner's `CLAUDE.md`, which contains LIVE credentials** (OAuth client
secrets and refresh tokens). "Store everything" + a persistent DB + Bifrost calls means creds
could be persisted to disk or shipped over the wire. That must not happen.

- **Never-persist list:** OAuth tokens, client secrets, API keys, passwords, private keys,
  `.env` values, anything read out of a `CLAUDE.md`/`.env`/Keychain. These are never written
  to the graph, the vector store, logs, or the bus.
- **Redact before every model call:** `Guard.redact(payload)` runs on *all* outbound model
  payloads. A credential never leaves the machine in a Bifrost request.
- **Keychain, not files:** EDITH's own secrets (Bifrost key, ElevenLabs key) live in the
  **macOS Keychain** via `keyring`. Loaded into memory at use, never logged.
- **Encryption at rest:** the Memory store is encrypted on disk.

### 6.2 Cost / token governance

An always-on daemon narrating every terminal is a textbook silent token-burner — even with
generous Bifrost limits. Governance is mandatory, not optional.

- **Budget:** a per-window token/cost budget tracked by Guard; `status` surfaces usage.
- **Per-event gating:** most bus events are handled **locally** (pattern match, cached state,
  no model). A model call happens only when an event clears the gate (owner addressed EDITH,
  ambiguity that needs reasoning, an explicit skill trigger). Narration of routine terminal
  activity is summarized locally and only escalated to a model on demand.
- **Tier discipline:** the Router defaults to the cheapest tier that can do the job (haiku
  first), escalating to sonnet/opus only when the task demands it — matching the owner's
  "cheap model first" rule.

### 6.3 Autonomy gate — "confirm risky, auto the rest"

`Guard.authorize(action) -> allow | ask | deny`.

```
AUTO (allow, no prompt) │ ASK first (confirm)              │ context
────────────────────────┼──────────────────────────────────┼──────────
 read repos              │ git push                         │ Leash loosens
 review PRs              │ PR merge                         │ slice-by-slice
 open apps               │ deletes                          │ as trust
 launch terminals        │ destructive shell                │ builds.
 cd into a repo          │ messaging people in owner's name │
 narrate                 │ any write to shared/external state│
```

Matches the owner's CLAUDE.md guardrails (no direct push to `main`, confirm shared-state
actions, no destructive shortcuts).

---

## 7. The 6 Slices + Build Order

Each slice ships something usable before the next starts. **Build order: 1→6.**

**Spec-depth column = spec maturity *as of Session 1*, not a permanent ceiling.** North-star
(0) and Memory+Brain (1) get DEEP specs this session; 2–6 are authored at INTERFACE level now
and deepened when their build session begins.

| # | Slice | One-line purpose | Interface to `edithd` | Spec depth (Session 1) |
|---|-------|------------------|-----------------------|------------------------|
| 1 | **Memory + Brain** | Persistent core: durable memory + the orchestrator decision loop. | `recall`/`remember`/`compact`; BRAIN consumes bus events, sequences calls. | **DEEP** |
| 2 | **PR-review skill** | First real autonomous action: find + review a PR, ask when unsure. | Skill `run(context)->result`; reads repos/Slack; ASK-gated on any write. | Interface |
| 3 | **Voice** | Wake word + STT in, Jarvis-style TTS out. | VoiceIO: emits `voice.wake`/`voice.utterance`; `speak(text)`. | Interface |
| 4 | **Session awareness** | Watch every OMC / Claude Code terminal; narrate. **Highest uncertainty — to prototype.** | SessionBus ingests OMC/CC events → `session.event`/`session.state`. | Interface (verify event source first) |
| 5 | **Router** | Tiered model selection over Bifrost; two-call latency masking. | `model_call(messages, tier_hint)->response`. | Interface |
| 6 | **Desktop control** | Voice-launch apps, drive terminals (`osascript`). | Skill(s) `run(context)`; ASK-gated on destructive shell. | Interface |

```
build ►  1 Memory+Brain ─► 2 PR-review ─► 3 Voice ─► 4 Session-aware ─► 5 Router ─► 6 Desktop
         (deepest spec)    (first action)          (prototype/verify)  (masking)  (osascript)
```

> Router (5) is the last-built shared dependency: slices 1–4 call it through the
> `model_call` contract from day one; the tiering/two-call *mechanics* land in slice 5. Until
> then a single-tier passthrough is acceptable.

---

## 8. Reality-Checks (from the actual machine)

- **Repos** live in `~/gitstuff/` (e.g. `~/gitstuff/concorde_lib`) — NOT `github/concord/lib`.
- **iTerm is NOT installed.** Available: **Spotify, `say`, `osascript`, `ffmpeg`, `sqlite3`,
  `uv`, `node`, `bun`, `docker` (Rancher).** `ollama` is **NOT** installed → any local model
  path must not assume ollama.
- **System Python is 3.9.6** — too old. Use **uv-managed Python 3.11+**.
- **Session-awareness event source is an ASSUMPTION.** Slice 4 taps OMC via **hooks** +
  `.omc/logs` / `.omc/state/sessions/{id}/`. **Verify this event source exists and is
  parseable before building** — it is the highest-uncertainty item in the whole plan.
- **Desktop control** must lean on `osascript` / `say` (present); the terminal driver is TBD
  because iTerm is absent (Terminal.app or a spawned shell instead).

---

## 9. Open Questions (surfaced for owner)

1. **Vector index pick** — ~~is Kuzu's native vector index mature enough, or pair with
   LanceDB / sqlite-vec?~~ **RESOLVED (Slice 1 build, Session 2): Kuzu (graph) + `sqlite-vec`
   (vectors).** Kuzu 0.11.3 (latest) HNSW is build-once — no incremental inserts — which breaks
   continuous learning. sqlite-vec supports incremental inserts and `sqlite3` is already present.
   See `01-memory-brain.md` §Storage decision.
2. **Terminal driver** — with iTerm absent, do we drive Terminal.app via `osascript`, or spawn
   and manage our own shell for OMC launches? Decided in Slice 6 (prototype in Slice 4).
3. **Graph server escalation** — does any slice actually justify Neo4j-over-Kuzu, or does
   embedded hold for v1?

---

*Cross-refs: continuity + completion rules → `../SESSION-PROTOCOL.md`. Slice specs →
`01-` … `06-` (follow `_TEMPLATE.md`).*
