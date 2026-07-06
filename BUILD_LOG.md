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

---

## Session 2 — 2026-07-06 — Slice 1 build: Memory store foundation (strict TDD)

Branch: `build/slice-1-memory-brain`. Iron law honored throughout: red → verify the
right failure → minimal green → refactor. Real embedded Kuzu 0.11.3, no mocks.

### What shipped (all green)
- **Project scaffold** — `uv` project, Python 3.11.14, `kuzu`; dev `pytest`/`ruff`/`pyright`
  all configured in `pyproject.toml` with `.venv` excluded from ruff+pyright. `.env.example`
  with Bifrost placeholders (no real secrets). Package `edith/memory/` + `tests/`.
- **Graph store** (`store.py`) — Owner/Project/Repo/Person/Fact nodes +
  `works_on`/`owns`/`knows`/`relates_to` edges. `remember` (idempotent upsert),
  `recall` (substring anchor match + 1-hop `relates_to` traversal). Sync (documented).
- **Embeddings** (`embeddings.py`) — `Embedder` Protocol + `LocalEmbedder`
  (fastembed / all-MiniLM-L6-v2, 384-dim, offline).
- **Vector recall** (`vector.py`) — `VectorMemoryStore` with Kuzu native HNSW,
  `build_vector_index()` + `semantic_recall(query, k)`.
- **Never-persist secrets filter** (`secrets.py`) — runs FIRST in `remember`; strips
  labelled secrets / PEM / provider tokens to `[REDACTED]`, keeps the fact.
- 12 tests: `uv run pytest` → 12 passed · ruff clean · pyright 0 errors.

### Commits
`0d60308` scaffold · `9d94f19` graph store · `7deb7a8` embedder+vector ·
`4c9c7b7` secrets filter · `2e3aa45` build-once vector index + verified reopen recall.

### Decisions / surprises
- **Sync over async** this run — no async consumer yet, Kuzu is blocking. Contract goes
  async when edithd lands (noted in code).
- **fastembed, not sentence-transformers** — same model/dim, no torch, light install.
- **BIG surprise — Kuzu 0.11.3 vector index is BUILD-ONCE, not rebuildable.**
  `DROP_VECTOR_INDEX` + `CREATE_VECTOR_INDEX` under the same name fails
  "Index … already exists" *even within one session* (stale catalog entry survives the
  drop). Verified with isolated probes. The persisted index IS directly queryable after a
  fresh reopen with no rebuild (proven). This confirms the spec's static-HNSW maturity
  caveat and is the concrete trigger for the sqlite-vec fallback → **owner decision needed**
  (see STATE.md blockers). Removed the drop-on-write path from `remember` that this bug had
  briefly regressed.
- Substituted the secrets filter for the optional `compact()` stub — the filter is a
  safety property of code we built; `compact()` needs Session/Conversation tables + a
  working-context buffer that don't exist yet (deferred honestly).

### Method note
Smoke-tested both network deps (Kuzu extension download, fastembed model fetch) BEFORE
building the vector layer, per the "discover env facts early" discipline — both worked, so
step 3 was unblocked. Committed after each green step (data-loss insurance).

### Next session (Session 3)
Continue Slice 1: Brain loop skeleton (bus + recall→decide→remember, Router passthrough),
then edithd lifecycle + Control API. Add Session/Conversation node tables, then `compact()`.
Get the vector re-index decision (build-once vs sqlite-vec) and Bifrost creds from owner.

---

## Session 3 — 2026-07-06 — Vector layer swap: Kuzu HNSW → sqlite-vec

**Goal:** Replace the build-once Kuzu HNSW vector index with embedded `sqlite-vec` so
`remember()` supports incremental inserts (a fact told moments ago is immediately recallable).
Implements the revised §Storage decision: **Kuzu keeps the graph, sqlite-vec owns the vectors.**
Strict TDD.

### What changed
- `edith/memory/vector.py` — full rewrite. `VectorMemoryStore` opens a sibling sqlite file
  (`<db>.vec.sqlite`), loads the `sqlite_vec` extension, and keeps a `vec0` virtual table plus
  a `fact_map(rowid ↔ fact_id, text)` companion. `remember` writes the graph node (Kuzu) then
  the vector row + id-map row (sqlite, one transaction). `semantic_recall` is a sqlite-vec KNN
  join. `build_vector_index()` retained as a **no-op** (inserts are incremental). Dropped the
  Kuzu `embedding FLOAT[384]` column and `_ensure_embedding_column` — vectors leave Kuzu.
- `store.py` / `embeddings.py` / `secrets.py` — **unchanged** (public interface preserved).
- `pyproject.toml` / `uv.lock` — `uv add sqlite-vec` (0.1.9).
- `tests/test_vector_recall.py` — **+1 new** defining test, **1 rewritten**.

### The defining test (RED first, then green)
`test_fact_remembered_after_index_exists_is_recalled_immediately`: remember f1 → build →
remember f2 *after* the index exists → recall returns f2. Watched it fail RED on build-once
Kuzu: `RuntimeError: Cannot set property vec in table embeddings because it is used in one or
more indexes.` — Kuzu can't even insert a new embedded row once the index exists. Green on
sqlite-vec. This is the exact "never re-explain context" capability Kuzu lacked.

### id-mapping
`fact_map` sqlite table maps sqlite-vec integer `rowid` ↔ Kuzu Fact string `id` (+ denormalized
text for the recall shape), written in the same `remember()` as the vector row and graph node.

### Decisions / surprises
- **No cross-engine 2PC — stated honestly.** Two embedded engines can't 2-phase-commit. Graph
  write lands first; sqlite writes run in one transaction; any `sqlite3.Error` rolls back the
  sqlite side and re-raises (no bare except, no swallowing) so a desync surfaces to the caller.
- **One test rewritten, not code-worked-around.** `test_semantic_recall_empty_before_index_build`
  asserted `semantic_recall == []` before a build — that `== []` *was* the build-once
  limitation being removed. Rewrote it to `test_semantic_recall_works_without_build_step`
  (deliberate deviation, flagged in the test). Net 13 green (11 unchanged + 1 rewritten + 1 new).
- Smoke-tested sqlite-vec extension loading + KNN round-trip on-machine BEFORE building (same
  discipline as Session 2's kuzu/fastembed check) — macOS stdlib `sqlite3` allows extension
  loading here, so no blocker.

### Verification (fresh)
- `uv run pytest` → **13 passed**
- `uv run ruff check edith tests` → **All checks passed**
- `uv run pyright edith` → **0 errors, 0 warnings**

### Next session (Session 4)
Continue Slice 1: Brain loop skeleton (bus + recall→decide→remember, Router passthrough), then
edithd lifecycle + Control API. Add Session/Conversation node tables, then `compact()`.
Vector re-index blocker is now RESOLVED (sqlite-vec incremental). Still need Bifrost creds.

---

## Session 4 — 2026-07-06 — Slice 1: bus + Router/Bifrost adapter + Brain loop (strict TDD)

Built the three components that turn the Memory store into a working core loop, each red→green
on `build/slice-1-memory-brain`. Baseline was **14 passed** (docs said 13; reconciled
empirically). Ended **29 passed, 1 skipped** (+ the live smoke green under `--run-live`).

### 1. Event bus (`edith/bus/`)
In-process async pub/sub, north-star envelope `Event{topic, ts, source, payload}`.
`async publish` awaits all matching handlers via `asyncio.gather` (deterministic, no
`sleep(0)` flakiness), topic-filters, no-ops with no subscribers. 4 tests, RED on missing module.

### 2. Router + Bifrost adapter (`edith/router/`)
`async model_call(messages, tier_hint) -> ModelResponse` over the Anthropic-compatible gateway
(`POST {base}/v1/messages`). `httpx.AsyncClient` **constructor-injected** → `MockTransport` seam
tests request construction / response parse / tier→model map with **no live call**. Retries via
**tenacity** (`retry_if_exception`: `TransportError` or status ≥ 500; 3 attempts; exp backoff;
`reraise=True`); **4xx raises immediately** — both directions tested (503-then-200 → 2 calls;
400 → 1 call, raises). One `@pytest.mark.live` smoke (skipped by default; `.env` loaded only on
the `--run-live` path so the billable call never fires on a plain `pytest` — cost rule) hit real
Bifrost: **200, non-empty text, max_tokens=8**. Model ids = the task's verified defaults. 6 unit
+ 1 live. Added `BIFROST_MODEL_*` to `.env.example` + the real gitignored `.env`.

### 3. Brain loop (`edith/brain/`)
Core loop on `voice.utterance`: `recall` → assemble (preamble + recalled facts + utterance) →
**redact** (`secrets.sanitize_text` over every message) → `Router.model_call` (single-tier
SONNET passthrough, north-star §7) → **remember** the exchange (redacted first) → publish
`brain.decision`. Memory/Router consumed via `Protocol`s. 4 tests with injected fakes over the
real bus.

### Decisions / notes
- **Redaction in Brain, not Router** — spec 05 puts the choke-point in Router *once Guard exists*;
  Guard isn't this slice, so building it = scope creep. Interim: Brain redacts. Deviation logged.
- **Key from `.env`, not Keychain** — task's verified contract overrides spec's Keychain-only;
  Keychain = daemon-bring-up work. Key never printed; `sk-bf-*` redacted in output.
- **Redaction-test RED found a real gap.** Brain was building the remembered Fact from the *raw*
  utterance (fake Memory doesn't sanitize like the real store) → planted secret leaked into
  what-was-remembered. Root-cause fix: redact the exchange text in Brain (defense-in-depth), not
  a test hack.
- Sync Memory called directly from async Brain (no real concurrency yet; `asyncio.to_thread` is
  the noted future step). `compact()` still deferred.

### Verification (fresh)
- `uv run pytest` → **29 passed, 1 skipped** · live smoke `--run-live` → **1 passed**
- `uv run ruff check edith tests` → **All checks passed**
- `uv run pyright edith` → **0 errors, 0 warnings**

### Next session (Session 5)
**`edithd` daemon lifecycle + Control API** (unix-socket `pause`/`resume`/`kill`/`status`,
launchd plist, encrypted-volume mount, pause-suspends-Memory). Then `compact()` (needs
Session/Conversation node tables + working-context buffer) and **Guard**. Rotate the Bifrost key.
