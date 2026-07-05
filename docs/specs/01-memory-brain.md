# 01 — Memory + Brain

> **Honest-framing reminder:** no unicorns. "Unlimited context" = memory + retrieval +
> compaction; "two agents in one inference" = orchestration of two model calls (fast masks
> slow). If a section here implies a capability that doesn't exist, fix the section.
>
> This slice follows the shape below. Architecture-level interfaces + cross-cutting rules are
> fixed in `00-north-star.md` — **do not restate them, reference them.** This file adds
> *implementation* depth for this slice only. This is the **DEEP** spec (per north-star §7):
> it goes to implementation-decision depth. Slices 2–6 are interface-level.

## Terminology (glossary)

| Term | Meaning |
|------|---------|
| **EDITH** | The system. Always-on local-first macOS assistant. |
| **edithd** | The daemon process that runs everything under the hood. |
| **bus** | In-process event/message bus; components `publish`/`subscribe`. |
| **Guard** | Cross-cutting enforcement: `redact`, `authorize` (allow/ask/deny), budget. |
| **Router** | `model_call(messages, tier_hint) -> response` over the Bifrost adapter. |
| **Memory** | Graph + vector store: `recall` / `remember` / `compact`. |
| **Brain** | The orchestrator decision loop. Consumes bus events, plans, sequences calls. |
| **working context** | The in-RAM assembled context for the current conversation/session — the thing that fills up and gets compacted at ~50%. Distinct from durable Memory on disk. |
| **subgraph** | The relevant slice of the graph pulled for one query (traversal from anchor nodes). |
| **compaction** | Summarize-and-shrink of working context when it hits the ~50% threshold. |
| **tier** | Model size class the Router selects: haiku / sonnet / opus. |

---

## Purpose

Memory + Brain is the persistent core every other slice plugs into. **Memory** is EDITH's
durable model of the owner's world — a property graph of projects, repos, PRs, people, and the
facts/preferences/events connecting them, paired with a vector index for semantic recall.
**Brain** is the orchestrator loop that, on every interaction, recalls the relevant slice of
that world, assembles a working context, decides what to do (answer / ask / dispatch a skill),
and writes new facts back — then compacts working context before it blows the window. Together
they deliver the "never re-explain context" experience: durable recall that *feels* like an
unlimited window without being one.

The usable thing this slice ships: a running `edithd` that can be told a fact in one session
("the onboarding-portal Asana bug was the service account not being shared"), and recall and use
it in a later session without being re-told — proven by a recall test across daemon restarts.

## Scope

**In:**
- The Memory store: graph schema (nodes/edges), vector index, encrypted-at-rest on-disk layout,
  and the `recall` / `remember` / `compact` implementations.
- The Brain orchestrator loop: intent → recall → assemble working context → decide → act →
  remember → compact. Enforces the Guard autonomy gate; dispatches Skills; calls Router.
- The `edithd` daemon lifecycle: launchd supervision, startup/shutdown ordering, the Control API
  unix-socket server, and pause/resume/kill/status semantics (including what pause does to Memory).
- The internal bus wiring for these two components (which topics Brain and Memory produce/consume).

**Out (deferred / another slice's job):**
- VoiceIO (Slice 3) — Brain consumes `voice.utterance` but does not implement STT/TTS.
- The Router tiering + two-call latency-masking *mechanics* (Slice 5) — Brain calls the
  `model_call` contract; a single-tier passthrough is acceptable until Slice 5 (per north-star §7).
- Concrete Skills (PR-review = Slice 2, Desktop = Slice 6). Brain dispatches to the uniform
  `Skill.run(context)` contract; this slice ships **one trivial built-in skill** (`echo`/`remember`)
  purely to exercise the dispatch path.
- SessionBus event *production* (Slice 4). Brain subscribes to `session.event`/`session.state`
  and records them into Memory, but does not implement the OMC/CC watcher.

## Interface to edithd

- **Inputs (calls received / events consumed):**
  - Bus: `voice.utterance` (owner said something), `session.event` / `session.state` (a watched
    terminal changed), `skill.result` (a dispatched skill finished), `budget.warning`.
  - Control API (from the daemon core, forwarded off the unix socket): `pause`, `resume`, `kill`,
    `status` — Brain owns the runtime-state transitions these imply.
- **Outputs (events published / return values):**
  - Bus: `brain.decision` (what Brain chose to do, for observability + the menu-bar `last_event`),
    `guard.blocked` is published *by Guard* but Brain reacts to it.
  - `Memory.recall(query) -> context`, `Memory.remember(facts | edges) -> ()`,
    `Memory.compact() -> ()` (signatures **locked in north-star §4.3** — referenced, not restated).
- **Bus events (envelope shape):** the north-star envelope `{ topic, ts, source, payload }`
  applies unchanged. Payloads are **already Guard-redacted** before they reach the bus (north-star
  §6.1). Topics this slice adds detail to:

  | Topic | Direction (this slice) | Payload sketch |
  |-------|------------------------|----------------|
  | `voice.utterance` | consume | `{ text, confidence }` |
  | `session.event` | consume | `{ session_id, kind, summary }` (redacted) |
  | `session.state` | consume | `{ session_id, state, active_repo }` |
  | `skill.result` | consume | `{ skill, ok, result_summary }` |
  | `brain.decision` | publish | `{ intent, action, tier_used?, asked?: bool }` |

- **Control contracts:** this slice **implements the daemon side** of the four locked Control API
  commands (north-star §4.2). It does not add new commands. `status` returns the locked shape
  `{ state, active_skill, budget_used, last_event }`; Brain supplies `state`, `active_skill`, and
  `last_event`, Guard supplies `budget_used`.

---

## The core loop — "never re-explain context"

This is the value of the whole system, so it is specified concretely. On **every** interaction
that clears the Guard budget gate (north-star §6.2), Brain runs one pass of:

**recall → assemble → decide/answer → remember → compact.**

```
                          ┌─────────────────────────────────────────────┐
   voice.utterance /      │                 BRAIN LOOP                    │
   session.event  ──────► │                                              │
        (intent)          │  1. RECALL                                    │
                          │     Memory.recall(query) does 3 things and    │
                          │     fuses them:                               │
                          │       (a) graph traversal from anchor nodes   │
                          │           (owner, active Project/Repo/Session)│
                          │       (b) vector similarity over Fact/        │
                          │           Conversation/PR text embeddings     │
                          │       (c) recency boost (last-N sessions,     │
                          │           touched-recently nodes)             │
                          │            │                                  │
                          │            ▼                                  │
                          │  2. ASSEMBLE working context                  │
                          │     = system preamble                         │
                          │       + owner profile/preferences (pinned)    │
                          │       + recalled subgraph (as facts/edges)    │
                          │       + top-k semantic hits                    │
                          │       + running conversation buffer           │
                          │     (Guard.redact runs on the whole thing)    │
                          │            │                                  │
                          │            ▼                                  │
                          │  3. DECIDE / ANSWER                            │
                          │     Guard.authorize(action) → allow|ask|deny  │
                          │       allow → answer or dispatch Skill        │
                          │       ask   → speak a confirm question        │
                          │       deny  → refuse + say why                │
                          │     answer via Router.model_call(msgs, tier)  │
                          │            │                                  │
                          │            ▼                                  │
                          │  4. REMEMBER                                   │
                          │     extract new Facts/edges from the turn,    │
                          │     Memory.remember(...) writes them back     │
                          │     (never-persist filter runs FIRST)         │
                          │            │                                  │
                          │            ▼                                  │
                          │  5. COMPACT (conditional)                     │
                          │     if working_context_tokens ≥ 50% window:   │
                          │        summarize oldest span → 1 Session/      │
                          │        summary node in graph, drop raw turns  │
                          │        from working context                    │
                          └─────────────────────────────────────────────┘
```

**Why this feels like unlimited context.** The window never actually grows. What changes is that
(a) durable facts survive across sessions and restarts, so nothing is re-explained, and (b) each
turn only loads the *relevant* subgraph + top-k hits, not the whole history — so the working
context stays small and fresh. Compaction folds old raw turns into durable summaries the moment
the buffer gets heavy, so a multi-hour session never hits the wall. This is memory + retrieval +
compaction (north-star's honest framing), not an infinite window.

**Concrete walk-through (the onboarding-portal example).**

```
Session A (Tuesday):
  owner: "the onboarding-portal Asana 'Unknown object' error was the service
          account not being shared on the template."
  → RECALL: anchors on Project{onboarding-portal}; thin subgraph.
  → REMEMBER: writes Fact{"Unknown object = SA not shared on template"}
              --relates_to--> Project{onboarding-portal}, and an Event of the fix.

Session B (Friday, fresh daemon restart in between):
  owner: "why is onboarding-portal throwing Unknown object again?"
  → RECALL: graph traversal hits Project{onboarding-portal} → the Fact from Tuesday;
            vector hit on "Unknown object" corroborates.
  → ASSEMBLE: the Fact is in working context.
  → ANSWER: "Last time this was the service account not being shared on the Asana
             template — check that first." — with NO re-explanation from the owner.
```

---

## Data model (the graph — EDITH's memory of the owner's world)

Property graph in Kuzu (typed node + rel tables, Cypher). The schema is deliberately small and
relationship-heavy; it is the owner's dev-world, not a general knowledge base.

### Node types

| Node | Key properties | Meaning |
|------|----------------|---------|
| **Owner** | `id`, `name`, `email` | The one owner. Singleton anchor of all recall. |
| **Project** | `id`, `name`, `status` | A unit of work (e.g. onboarding-portal). |
| **Repo** | `id`, `path`, `remote` | A git repo (paths under `~/gitstuff/`). |
| **PR** | `id`, `number`, `title`, `state` | A pull request. |
| **Person** | `id`, `name`, `handles{slack,github}` | A colleague (e.g. Tavishi, Nate). |
| **Session** | `id`, `started_at`, `ended_at`, `summary` | One conversational/work session. Compaction targets. |
| **Conversation** | `id`, `turn_span`, `summary`, `embedding` | A summarized span of turns (product of compaction). |
| **Fact** | `id`, `text`, `embedding`, `confidence`, `source`, `learned_at` | An atomic durable claim about the world. |
| **Preference** | `id`, `text`, `scope`, `embedding` | A stable owner preference/guardrail (pinned into every context). |
| **Event** | `id`, `kind`, `text`, `ts`, `embedding` | Something that happened (a fix, a push, a review). |

`embedding` lives on the node types we do semantic recall over: **Fact, Preference, Conversation,
Event, PR** (title/body). Structural-only nodes (Owner, Repo, Person) are reached by traversal.

### Edge types

| Edge | From → To | Meaning |
|------|-----------|---------|
| `works_on` | Owner → Project | Owner is engaged on a project. |
| `owns` | Project → Repo | Project's code lives in a repo. |
| `has_pr` | Repo → PR | A PR against a repo. |
| `authored_by` | PR → Person | Who wrote it. |
| `reviewed_by` | PR → Person | Who reviewed it. |
| `mentions` | Conversation/Fact/Event → any | Referenced this node. |
| `relates_to` | Fact → Project/Repo/PR/Person | What a fact is about. |
| `derived_from` | Conversation → Session; Fact → Conversation | Provenance of a summary/fact. |
| `occurred_in` | Event → Session | When/where an event happened. |
| `prefers` | Owner → Preference | Owner holds this preference. |
| `knows` | Owner → Person | Owner works with this person. |

```
                                   ┌──────────┐
                    prefers ◄──────│  Owner   │──────► knows ──► ┌────────┐
                  ┌────────┐       └────┬─────┘                  │ Person │
                  │Preference│          │ works_on               └───▲──┬─┘
                  └──────────┘          ▼                            │  │
                                  ┌──────────┐  owns   ┌──────┐ has_pr │  │ authored_by
                                  │ Project  │────────►│ Repo │────────┼──┘ / reviewed_by
                                  └────┬─────┘         └──────┘        │
                              relates_to│                    ┌─────┐   │
                        ┌──────┐        │                    │ PR  │◄──┘
                        │ Fact │◄───────┘                    └──▲──┘
                        └──▲───┘  mentions ───────────────────►(any)
             derived_from │                     ┌─────────┐
                          │            occurred_in│  Event  │
                    ┌───────────┐      ┌──────────┴──▲──────┘
                    │Conversation│◄────┤ derived_from │
                    └─────▲──────┘     │      ┌───────┴───┐
                          └────────────┴──────┤  Session  │
                                              └───────────┘
   (nodes carrying `embedding` for vector recall: Fact, Preference, Conversation, Event, PR)
```

### Storage decision — RESOLVES north-star Open Question #1

**Decision: Kuzu native HNSW vector index (primary). sqlite-vec as the documented fallback.**

Verified against current Kuzu docs (context7 `/kuzudb/docs`, this session): Kuzu ships a
**VECTOR extension** providing a native **disk-based HNSW** index —
`CALL CREATE_VECTOR_INDEX(...)` / `CALL QUERY_VECTOR_INDEX(table, index, query_vec, k)` — over
`FLOAT[]`/`DOUBLE[]` node properties, with cosine/L2 metrics and standard HNSW knobs (`efc`,
`efs`, `mu`, `ml`). It exists and is buildable today (no unicorn).

**Why Kuzu-native wins for v1:**
- **One engine, not two.** Graph traversal and vector search live in the same DB, same
  transaction, same file. `recall` can fuse "traverse the subgraph" and "top-k similar Facts" in
  Cypher without a cross-store join or a second connection to keep consistent. This directly
  matches the owner's "don't over-engineer / reach for what I already know first" rules and the
  north-star "no server" default.
- Embeddings sit as a property on the very nodes we traverse, so a semantic hit *is* already a
  graph node — no id-mapping layer between a vector store and the graph.

**The honest maturity caveat (the reason a fallback is named).** As of the pinned Kuzu version,
the retrieved docs expose only `CREATE_VECTOR_INDEX` / `QUERY_VECTOR_INDEX` and **no incremental
in-place update API** — strongly implying the HNSW index is **static** (built over the rows present
at build time; new vectors become searchable only after a **rebuild**). This is a version-dependent
assumption to **re-verify at build time**, not an eternal fact — but it is the conservative reading,
and our design degrades gracefully if it turns out looser. New Facts/Events written by `remember`
are queryable structurally (Cypher) immediately; they become vector-searchable after the next
rebuild. We accept and design around this (see Retrieval + compaction below) rather than pretend it
away.

**Fallback (documented, not default): Kuzu (graph) + `sqlite-vec` (vectors).** Chosen over LanceDB
because **`sqlite3` is already present on the machine** (north-star §8 reality-check), it is the
lightest possible embedded vector option, and it supports incremental inserts (no rebuild step) —
which would directly resolve the maturity caveat if it bites. The cost is a second store and an
id-mapping layer (vector row ↔ graph node id). We adopt this fallback only if the rebuild approach
proves too costly in practice (measure first — owner's rule). **We do not adopt Neo4j** for this
slice; north-star Open Question #3 (server escalation) stays "embedded holds for v1."

**Encryption at rest — stated honestly.** Kuzu does **not** provide native at-rest encryption, and
neither does sqlite-vec by default. Do **not** write "the DB encrypts the store" — that is the
capability-that-doesn't-exist the north-star forbids. The real mechanism:

- The entire Memory store (Kuzu DB directory + any fallback file) lives inside a **dedicated
  encrypted APFS volume / sparse bundle** that `edithd` mounts at startup.
- The **volume unlock key lives in the macOS Keychain** (`keyring`), fetched at daemon start and
  never written to disk or logged.
- FileVault is assumed as table-stakes (full-disk) but is **not sufficient alone** — it is
  decrypted whenever the owner is logged in. The dedicated volume gives EDITH a store that is
  encrypted independent of the login session and can be unmounted on `kill`.
- If a future Kuzu release ships native encryption, we revisit; until then, filesystem-level is
  the mechanism.

## Retrieval + compaction

### Retrieval strategy (`recall`)

`recall(query) -> context` fuses three signals into one ranked working-context set:

1. **Graph traversal (structural).** Start at anchor nodes — `Owner`, the currently-active
   `Project`/`Repo`/`Session` (tracked in Brain runtime state) — and traverse 1–2 hops along
   `works_on`/`owns`/`has_pr`/`relates_to`/`mentions`. This pulls the *structurally relevant*
   neighborhood cheaply and deterministically (no model, no embedding).
2. **Vector similarity (semantic).** Embed the query locally (sentence-transformers,
   `all-MiniLM-L6-v2`, matching Kuzu's documented example — small, local, no cloud, no cost) and
   run `QUERY_VECTOR_INDEX` for top-k over Fact/Preference/Conversation/Event/PR embeddings. Catches
   things the traversal misses (a relevant Fact not yet edge-connected to the active anchors).
3. **Recency boost.** Nodes touched in the last N sessions, and the last N Conversation summaries,
   get a score bump. Recent work is disproportionately what the owner means.

**Fusion + freshness handling.** Score = weighted blend (traversal-adjacency + vector-similarity +
recency); return the top set under a token budget. **Because the HNSW index is static (see storage
decision), `recall` also runs a cheap structural Cypher scan for Facts written since the last index
rebuild** (`learned_at > last_rebuild_ts`) and merges them in — so a fact learned 30 seconds ago is
recalled even before it is vector-indexed. This is the concrete design-around for the maturity
caveat, not a workaround bolted on later.

### Compaction strategy (`compact`)

Triggered when working-context tokens reach **~50%** of the model window (north-star's stated
threshold). Working context is ordered oldest→newest; conversation turns are the compressible part
(the pinned owner profile/preferences and the recalled subgraph are not dropped).

1. Take the **oldest span** of raw turns (leave the most recent turns verbatim for coherence).
2. Summarize that span with a **cheap tier (haiku)** into a compact summary — this is a
   latency/cost-appropriate model call, gated like any other.
3. Persist the summary as a **`Conversation` node** (`derived_from` the `Session`), embed it, and
   extract any durable **Facts** (`derived_from` the Conversation).
4. **Drop the raw turns** from working context; the summary node now represents them and is
   recallable next turn.

**Vector-index rebuild cadence.** Because Kuzu HNSW is static, the vector index over the mutated
node tables is rebuilt on a **debounced schedule** — after a compaction pass and/or on an idle
timer (e.g. every M minutes of inactivity, or after K new embeddable nodes), never on every write.
Rebuild is a background task on the bus; structural recall (§Retrieval fusion) covers the gap so
recall is never *wrong* between rebuilds, only slightly less semantically complete. If rebuild cost
is ever measured as too high for the store size, that is the trigger to switch to the sqlite-vec
fallback (incremental inserts, no rebuild).

## Brain / Orchestrator loop — implementation depth

Brain is a bus subscriber with a small runtime state machine. Its place on the internal bus:
it **subscribes** to `voice.utterance`, `session.event`, `session.state`, `skill.result`,
`budget.warning`; it **publishes** `brain.decision`.

```
   bus events ──► [ Guard budget gate ] ──► handle locally?  ──yes──► update state, done (no model)
                        (north-star §6.2)         │no
                                                  ▼
                                       RECALL ─► ASSEMBLE ─► Guard.authorize(action)
                                                                 │
                                        ┌────────────────────────┼───────────────────┐
                                     allow                      ask                 deny
                                        │                        │                    │
                            answer / dispatch Skill      speak confirm Q       refuse + reason
                            via Router.model_call         (await owner)         publish guard.blocked
                                        │
                                    REMEMBER ─► COMPACT (if ≥50%) ─► publish brain.decision
```

- **Intent consumption.** Not every bus event earns a model call. Most `session.event`s are
  narration and are **summarized locally** and recorded to Memory with no model (north-star §6.2
  per-event gating). An event escalates to the full loop only when it clears the gate: owner
  addressed EDITH, genuine ambiguity, or an explicit skill trigger.
- **Planning.** For a cleared intent, Brain builds the working context (recall + assemble), then
  asks the model (via Router) for a plan/answer. It does not implement its own planner LLM prompt
  library here beyond a system preamble; skill-specific planning lives in the Skills.
- **Guard autonomy gate.** Every action that would touch the world runs through
  `Guard.authorize(action) -> allow | ask | deny` (north-star §6.3, mapping locked there). `ask`
  surfaces as a spoken confirmation via `speak()`; Brain blocks on the owner's answer before acting.
- **Skill dispatch.** Brain matches intent to a `Skill` by its declared `triggers`, checks
  `needs_confirmation` against the autonomy gate, and calls `Skill.run(context)`; the result comes
  back as a `skill.result` bus event, folded into the next remember pass. This slice ships only the
  trivial built-in skill to prove the path; real skills are Slices 2/6.
- **Router use.** All model calls go through `Router.model_call(messages, tier_hint)` (locked
  §4.3). Brain supplies a tier hint (compaction → haiku; a hard review → sonnet/opus) but the
  Router owns the final pick and, from Slice 5, the two-call latency-masking mechanics. Until then a
  single-tier passthrough is fine (north-star §7).

## edithd daemon lifecycle

`edithd` is the spine; this slice implements its lifecycle and the Control API server.

### Startup (ordered)
1. launchd starts `edithd` (native, uv-managed Python 3.11+).
2. Fetch secrets from Keychain (`keyring`): Memory-volume unlock key, Bifrost key. Held in RAM only.
3. **Mount the encrypted Memory volume** using the Keychain key; open the Kuzu DB.
4. Bring up the internal bus.
5. Start subsystems in dependency order: Memory → Guard → Router(passthrough) → Brain → (later)
   VoiceIO/SessionBus. Each registers its bus subscriptions.
6. Start the **Control API server** on the unix domain socket (loopback HTTP fallback per §4.2).
7. Enter running state; publish an initial `brain.decision`/status so the menu-bar has a label.

### Shutdown (`kill`) — graceful
1. Stop accepting new intents; drain in-flight skill dispatches.
2. Run a final `compact()` so the current session's tail is summarized and durable.
3. Flush Memory, trigger a final vector-index rebuild if dirty, close the Kuzu connection.
4. Unmount the encrypted volume; zero the in-RAM keys.
5. Close the Control API socket; exit. launchd may restart per its policy (or stay down if killed).

### launchd (always-on)
A user LaunchAgent (`~/Library/LaunchAgents/com.gsapify.edithd.plist`), `KeepAlive` with a
restart throttle, `RunAtLoad`, logging to `~/gitstuff/EDITH`'s log path (redacted — see secrets).
Native, not a container (mic/osascript/app-launch can't run in Docker — north-star §5).

### Control API + pause semantics
The four commands are locked (north-star §4.2); this slice implements their daemon-side behavior:

| Command | edithd behavior |
|---------|-----------------|
| `pause` | Enter `paused` state: **halt all autonomous action and all model calls** (no Router calls, no skill dispatch). VoiceIO keeps listening for `resume`/`kill` only. |
| `resume` | Return to `running`. |
| `kill` | Graceful shutdown (above). |
| `status` | Return `{ state, active_skill, budget_used, last_event }` (locked shape). |

**Pause + Memory — explicit decision (a judgment call, stated per honest-framing):**
> **When paused, Memory does NOT record new facts.** Rationale: north-star only says pause halts
> model calls and autonomous action, and is silent on Memory. Recording is local and cheap, so the
> tempting answer is "keep recording." We deliberately choose the **privacy-respecting** reading: a
> manual pause most often means "don't capture this moment" (a sensitive call, a side conversation).
> Recording silently while the owner believes EDITH is paused would violate that intent. So on
> `pause`, the remember/compact steps are suspended along with model calls; the running conversation
> buffer up to the pause point is retained in RAM and resumes on `resume`. This is reversible if the
> owner later prefers "always record" — it's a config flag, not an architectural constraint.

## Dependencies

- **Other slices:** none must exist first — this is Slice 1, the base. It defines contracts that
  2–6 consume. It *calls* Router via `model_call` (single-tier passthrough acceptable until Slice 5)
  and *speaks* via a `speak()` stub until VoiceIO (Slice 3) — for this slice, `speak()` may route to
  macOS `say` as a placeholder (present on the machine).
- **Libraries (matching north-star §5):**
  - `kuzu` (embedded graph + native VECTOR extension) — Memory store.
  - `sentence-transformers` (`all-MiniLM-L6-v2`) — local embeddings, no cloud/cost.
  - `keyring` — Keychain access for the volume key + Bifrost key.
  - stdlib `asyncio` — the in-process bus + Control API socket server.
  - `sqlite-vec` — **only if** the documented fallback is triggered (not a default dependency).

## Tech choices

Defer to north-star §5 for the stack. Slice-specific additions/decisions:
- **Vector index:** Kuzu-native HNSW (resolves Open Question #1), sqlite-vec fallback — justified
  above.
- **Embedding model:** local `all-MiniLM-L6-v2` (384-dim) — small, fast, offline, matches Kuzu's
  documented vector-extension example; keeps recall free and private.
- **Encryption:** dedicated encrypted APFS volume, key in Keychain — filesystem-level, honestly
  stated (no native-DB-encryption claim).

## Autonomy & secrets notes

- **Autonomy gate (this slice):**
  - **AUTO:** `recall`, `remember` (of non-secret facts), `compact`, narrating locally, answering a
    question. These are reads/local-writes to the owner's own store.
  - **ASK:** any action Brain dispatches that *writes to shared/external state* (a skill that would
    push, merge, message someone) — but those skills are Slices 2/6; in this slice the gate is
    wired and exercised by the trivial skill, real ASK cases arrive with real skills.
- **Secrets (per north-star §6.1 — referenced, applied here):**
  - **EDITH ingests the owner's `CLAUDE.md`, which contains LIVE credentials** (OAuth client
    secrets, refresh tokens). These are **never persisted** — not to the graph, the vector store,
    logs, or the bus. A **never-persist filter runs FIRST in the REMEMBER step**, before anything
    is written: it detects and strips secret-shaped material (token/key/secret/private-key/`.env`
    patterns, Keychain values, anything from a `CLAUDE.md`/`.env`) and writes only the *sanitized*
    fact. Example (synthetic placeholders — never real values):
    - ingested line: `client_secret: GOCSPX-EXAMPLE_FAKE_SECRET_DO_NOT_STORE`
    - what Memory stores: `Fact{"owner has a Google Workspace OAuth client configured"}` — the
      *fact of it*, never the secret itself.
  - **Redact before every model call:** `Guard.redact(payload)` runs on the whole assembled working
    context before it is handed to `Router.model_call` (north-star §6.1). A credential never leaves
    the machine in a Bifrost request even if one slipped into a raw turn.
  - **Keychain, not files:** EDITH's own secrets (Bifrost key, Memory-volume key) via `keyring`,
    loaded to RAM at use, never logged.
  - **Encryption at rest:** the Memory store lives on the encrypted volume (mechanism above).

## Cost / token notes

- **Most events cost nothing.** Per-event gating (north-star §6.2): routine `session.event`
  narration is summarized locally (string/pattern work, no model). A model call happens only when an
  intent clears the gate.
- **Embeddings are free/local** (sentence-transformers on-device) — recall's semantic step never
  hits Bifrost.
- **Compaction uses the cheapest tier (haiku)** — summarization is a haiku job, not opus.
- **Answering** defaults to the cheapest tier that can do the job (Router discipline, north-star
  §6.2); Brain only hints opus for genuinely hard reasoning.
- **Budget:** Guard tracks the per-window token/cost budget; `status.budget_used` surfaces it to the
  menu-bar. A `budget.warning` event makes Brain get conservative (prefer local handling, defer
  non-urgent model calls).

## Build steps (high-level, ordered)

1. **Daemon skeleton + Control API.** `edithd` process, the asyncio bus, the unix-socket Control API
   server implementing `pause`/`resume`/`kill`/`status` (locked shape). launchd plist.
2. **Encrypted store bring-up.** Create the encrypted APFS volume; Keychain key via `keyring`;
   mount-on-start / unmount-on-kill; open Kuzu inside it.
3. **Graph schema.** Create the node + rel tables (schema above) in Kuzu via Cypher DDL.
4. **Embeddings + vector index.** Wire `all-MiniLM-L6-v2`; `CREATE_VECTOR_INDEX` on the embeddable
   node tables; the debounced rebuild task.
5. **Memory API.** Implement `recall` (graph traversal + vector query + recency fusion + the
   since-last-rebuild structural scan), `remember` (never-persist filter FIRST → write nodes/edges →
   embed), `compact` (summarize oldest span → Conversation node → drop raw turns).
6. **Brain loop.** State machine + bus subscriptions; the recall→assemble→decide→remember→compact
   pass; Guard autonomy gate + budget gate integration; a single-tier `Router.model_call` passthrough
   and a `speak()`→`say` placeholder.
7. **Trivial built-in skill.** One `Skill` (e.g. `remember`/`echo`) to exercise dispatch +
   `skill.result` round-trip.
8. **Pause semantics.** Wire the "no record while paused" decision (config-flagged).
9. **Wire it end-to-end** and run the verification below.

## Verification / testing

Prove the two load-bearing behaviors — **recall across restarts** and **compaction** — with fresh
output at build time (not assumptions).

- **Recall across restart (the core promise):**
  1. Start `edithd`. Feed a fact ("onboarding-portal Unknown object = SA not shared on template").
  2. `kill` the daemon; confirm graceful shutdown + volume unmount.
  3. Restart. Ask "why does onboarding-portal throw Unknown object?" → EDITH answers *from the
     stored fact* with no re-explanation. **Expected:** the Tuesday fact appears in the assembled
     context (log the recall set) and in the answer.
- **Vector + structural fusion:** write a fact, immediately query (before an index rebuild) →
  confirm it is recalled via the since-last-rebuild structural scan. Then trigger a rebuild and
  confirm it is now returned by `QUERY_VECTOR_INDEX` too.
- **Compaction:** drive a long synthetic session past the ~50% threshold → confirm (a) a
  `Conversation` summary node is created with `derived_from` the Session, (b) raw turns are dropped
  from working context, (c) the summarized content is still recallable next turn. **Expected:**
  working-context token count drops after compaction while recall of the old content still succeeds.
- **Secrets never-persist:** ingest a synthetic `CLAUDE.md` line containing a fake `client_secret` →
  assert the raw secret is **absent** from the Kuzu DB, the vector store, logs, and any bus payload;
  assert only a sanitized Fact was written. Assert `Guard.redact` strips a planted secret from a
  model-call payload.
- **Control API + pause:** exercise `status` (locked shape returned), `pause` (assert no model calls
  and — per the decision — no new Memory writes occur while paused), `resume`, `kill`.

## Open questions

- **Rebuild cost vs. sqlite-vec fallback** — at what store size does Kuzu's static-HNSW rebuild
  cadence become too costly, forcing the sqlite-vec fallback? Resolve by **measuring** during build
  (owner's "measure before you optimize" rule), not by guessing now.
- **Compaction fidelity** — does haiku-summarized compaction lose facts the owner later needs? May
  need a "verify important facts were extracted" check. Resolve during the compaction verification.
- **Pause + Memory default** — this spec chose "don't record while paused." Owner to confirm that's
  the desired default vs. "always record" (config flag either way).
- **Anchor selection for recall** — how aggressively to expand traversal hops (1 vs 2 vs adaptive)
  before it pulls in noise. Tune against real recall quality once there's real data.

---

## Completion Record — Memory + Brain — <date>

> Fill this at session end per `../SESSION-PROTOCOL.md` §4 (canonical template lives there).
> Leave empty until the slice is built.

- **What shipped:**
- **How it works:**
- **Key decisions made during build:**
- **Deviations from spec + why:**
- **Files created / changed:**
- **Verification / tests run + results:**
- **Follow-ups / known gaps:**
