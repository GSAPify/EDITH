# 08 — Repo-knowledge Ingestion

> Architecture-level interfaces + cross-cutting rules are fixed in `00-north-star.md`
> and reused here (Memory `remember`, secrets `sanitize_text`, Router `model_call`). This
> file adds implementation depth for the ingestion slice only.

## Terminology (glossary)

See `_TEMPLATE.md`. Slice-specific: **choke-point** = the single place every fetched
text passes through `sanitize_text` before it can reach a model or the graph.

---

## Purpose

Populate EDITH's LIVE Memory graph from the owner's real work repos so the viewer
(spec 07) renders a real, dense graph instead of the `--demo` seed. Given the owner's
local `~/gitstuff` clones, the pipeline discovers the `patterninc` ones, reads their docs,
**redacts**, has Sonnet classify + Opus deep-extract, and writes `Repo`/`Person`/`Fact`
nodes and edges to `EDITH_DATA_DIR/memory.kuzu`.

## Scope

**In:** discovery from local clones; local doc fetch (README + CLAUDE.md, repo + `.claude/`)
plus best-effort `gh` metadata; the redaction choke-point; Sonnet/Opus extraction over an
injected Router; graph mapping to the existing schema; `python -m edith.ingest` CLI with
`--dry-run`/`--repos`/`--limit`/`--data-dir`; incremental skip; a stdout status report; the
one-time global `~/.claude/CLAUDE.md` owner-context ingest.

**Out:** the full contributed-repos live run (orchestrator triggers after review); cloning /
network repo discovery (we use local clones as ground truth); vector re-embedding tuning
(inherited from `vector.py`); scraping SellerCentral or any Amazon surface.

## Interface to edithd

- **Inputs:** none via the bus in this slice — invoked as a CLI / library
  (`run_ingest(...)`). A later slice can wrap it behind a Skill.
- **Outputs:** nodes/edges written to Memory via `remember`; an `IngestReport` returned +
  printed to stdout.
- **Bus events:** none this slice.
- **Control contracts:** none.

## Data model

Reuses the slice-1 schema (`edith/memory/store.py`), extended **additively**:

- `Repo(id, path, remote, name, summary, language, last_commit_date)` — four columns added.
- `Fact(id, text, learned_at, source)` — `source` added (`readme` / `claude_md` / `extraction`).
- `authored_by(FROM PR TO Person, FROM Repo TO Person)` — `Repo→Person` pair added (repo
  attributed to its owner from extraction).
- Edges used: `owns` (Project→Repo, when Opus names a parent project), `authored_by`
  (Repo→Person), `relates_to` (Fact→Repo, Fact→Person for the global owner-context facts).
- `Project` nodes come from the Opus `project` field (empty string ⇒ no Project node; the
  model returns "" when a repo has no clear parent initiative — honest, not synthesized 1:1).

Node ids are deterministic (`repo-<name>`, `person-<slug>`, `fact-<sha1[:12]>`) so re-runs
upsert idempotently.

## Dependencies

- **Other slices:** Memory (1) for `remember`/schema; secrets (1) for `sanitize_text`;
  Router (5) for `model_call`. No new runtime deps.
- **Libraries:** stdlib (`argparse`, `asyncio`, `hashlib`, `subprocess`), plus `httpx`
  (Router) and `kuzu` (Memory) already in the tree. `gh` is an optional external CLI.

## Tech choices

- **Discovery = local clones, not `author=<login>`.** The owner's Pattern commit identity
  differs from their `gh` login (`author=GSAPify` → 0 commits). Ground truth is a local
  clone whose `origin` points at `github.com[:/]patterninc/`. Offline, fast, accurate.
- **Router constructor-injected.** `run_ingest(router=...)` takes any `model_call`-shaped
  object, so unit tests use a deterministic fake — no live calls, no cost.
- **Bounded concurrency, serial writes.** Fetch+extract fan out under an `asyncio.Semaphore`;
  all Kuzu writes happen serially after `gather` (Kuzu is single-writer).

## Autonomy & secrets notes

- **Autonomy gate:** AUTO — read-only over the owner's own files + model calls; no external
  side effects. The full live run is gated on human review (orchestrator), not a runtime ask.
- **Secrets (redaction-first, north-star §6.1):** the choke-point is `redact.py`
  (`sanitize_text` over every fetched text) run BEFORE extraction (the model call) AND BEFORE
  `remember` (the graph write). Defence-in-depth: `extract._call` re-sanitizes message content
  at the model egress, `remember` re-sanitizes at the graph egress, and `IngestReport.render`
  sanitizes stdout. The owner's global `~/.claude/CLAUDE.md` (LIVE OAuth tokens) is redacted
  hard before becoming owner-context Facts. A planted-secret test asserts absence from BOTH the
  fake Router input and the graph; the live smoke verified the owner's real
  `client_secret`/`refresh_token` never reach the DB.

## Cost / token notes

- **Sonnet** runs on every repo (cheap summary + 0..1 relevance).
- **Opus** runs ONLY when relevance ≥ 0.4 (budget skip). `deep_max_tokens` is CLI-controllable
  (`--max-tokens`); the smoke used 128.
- Latency-first (north-star §6.2): cheapest tier that fits, Opus reserved for repos worth it.

## Build steps (high-level, ordered)

1. `discover.py` — patterninc clones from `~/gitstuff` (regex over `.git/config`, git for date).
2. `fetch.py` — local README + CLAUDE.md (root + `.claude/`) + best-effort `gh` metadata.
3. `redact.py` — `sanitize_text` over every field: the choke-point.
4. `extract.py` — Sonnet classify → Opus deep-extract (injected Router, budget-aware).
5. `graph_map.py` — extracted knowledge → Node/Edge; `map_and_remember` to the live store.
6. `pipeline.py` + `__main__.py` — orchestrate; `--dry-run`/incremental/report; global CLAUDE.md.

## Verification / testing

- `uv run pytest` — 97 tests (1 live-skipped): discovery, fetch, redaction (planted-secret
  RED→GREEN + direct `redact_docs` unit + the secrets-filter markdown-wrapper regression),
  extract (tier routing + budget skip), graph_map (temp Kuzu), pipeline (full/dry-run/filter/
  incremental/secret-safe report/global ingest).
- `ruff check edith tests` + `pyright edith` — clean.
- Live smoke: `python -m edith.ingest --repos agents agentsmith --data-dir <temp>
  --max-tokens 128` → 2 repos ingested (both Opus, relevance 0.95/0.72), 55 facts to a temp
  dir; secret-scan of the temp DB reads clean.

## The five decisions (chosen defaults)

1. **Discovery source** → local `patterninc` clones (not gh author). Ground truth, offline.
2. **Incremental skip key** → `Repo.last_commit_date` stored on the node, re-read on next run.
   No separate state file (reuse the graph). Skip when unchanged.
3. **Relevance threshold for Opus** → 0.4 (Sonnet-scored). Budget knob; tune later.
4. **Owner-context source** → global `~/.claude/CLAUDE.md`, chunked to ≤500-char Facts on an
   `Owner (global context)` Person node, `source=claude_md`, redacted hard.
5. **Concurrency** → `asyncio.Semaphore(4)` for fetch/extract; serial Kuzu writes.

## Open questions

- **Existing-DB migration.** The schema extension uses `CREATE ... IF NOT EXISTS`, which is a
  no-op on a pre-existing table. First-ever DB creation (the current case — no live db yet)
  applies the full schema cleanly. A pre-existing old-schema `memory.kuzu` would need
  `ALTER TABLE ... ADD` migrations before the full run. Resolve when/if a live db predates this.
- **Fact granularity.** README/CLAUDE.md currently become one clipped Fact each; the full run
  may want finer chunking. Revisit after the orchestrator's contributed-repos run.

---

## Completion Record — 08 repo-ingest — 2026-07-07

- **What shipped:** `edith/ingest/` (discover, fetch, redact, extract, graph_map, pipeline,
  `__main__`) + spec 08. Redaction-first ingestion of local patterninc clones into the live
  Memory graph, with a CLI, dry-run, incremental skip, budget-aware Sonnet/Opus, and a
  secret-safe status report. Additive schema growth in `store.py`. A real security bug in the
  shared `secrets.py` filter (markdown-wrapped assignments leaked their value) was found by the
  live smoke and fixed root-cause with regression tests.
- **How it works:** discover → fetch → **redact (choke-point)** → Sonnet classify → Opus deep
  (if relevant) → map → `remember`. Router injected; defence-in-depth redaction at model,
  graph, and stdout egress.
- **Key decisions:** the five above.
- **Deviations from spec:** none material. Added `secrets.py` fix (in scope: redaction is
  non-negotiable). All four node types (`Repo`/`Project`/`Person`/`Fact`) and all three edges
  (`owns`/`authored_by`/`relates_to`) are implemented and unit-tested; `Project`/`owns` only
  materialize when Opus names a parent project (it returned "" for the two smoke repos, so the
  smoke DB shows Project=0 — correct, the model found no clear parent initiative in those docs).
- **Files created / changed:** `edith/ingest/*` (new); `edith/memory/store.py`,
  `edith/memory/secrets.py` (extended); `tests/test_ingest_*.py`, `tests/test_secrets_filter.py`.
- **Verification:** 97 tests green (1 live-skipped), ruff/pyright clean, live smoke wrote 58
  nodes to a temp dir with a clean secret-scan.
- **Follow-ups / known gaps:** existing-DB migration (open question 1); Fact granularity;
  Skill wrapper for bus-triggered ingestion.

---

## Completion Record — 08 repo-ingest — weekly graph refresh, item 4 (2026-07-27)

- **What shipped:** the daemon now schedules its own weekly graph refresh in-process
  (`EdithDaemon`, `edith/daemon/edithd.py`), behind `enable_graph_refresh=False` (default off,
  so every existing test and the plain daemon are unaffected). Two pieces:
  1. An **injected-store seam** on `ingest_workspace` (`edith/ingest/workspace.py`): a new
     `store: MemoryStore | None = None` kwarg. When given, `ingest_workspace` writes through
     THAT handle and never closes it; when absent, behaviour is byte-for-byte unchanged (builds
     and closes its own `VectorMemoryStore`, exactly as before). This is what lets the daemon's
     refresh reuse Brain's own Memory handle instead of opening a second Kuzu connection
     in-process — the central constraint from the roadmap ("Kuzu embedded is single-process").
  2. **`tenacity` retry on `_gh_list_repos`** — mirrors the Router's `_post_messages` policy
     exactly (`stop_after_attempt(3)`, `wait_exponential(multiplier=0.2, max=2.0)`,
     `reraise=True`), retrying only `subprocess.CalledProcessError`. This is for flakiness, NOT
     rate limits — the pass is ~13 paginated calls for patterninc / 1 for ampmedia, nowhere
     near the 5000/hr ceiling (confirmed in the roadmap's evidence).
- **The scheduled job itself:** an `asyncio.Task` started in `EdithDaemon.start()`, cancelled in
  `stop()`. Runs the model-free passes only — `ingest_workspace("patterninc")`,
  `ingest_workspace("ampmedia")`, then `backfill_embeddings()` — through the SAME Memory handle
  Brain uses. **Deep extraction (Opus per repo, ~2600 calls) is explicitly OUT OF SCOPE.**
  Interval and the refresh callable are both constructor-injectable
  (`graph_refresh_interval_seconds`, `graph_refresh_fn`) so tests never wait a real week or
  touch the network/`gh`.
- **Refresh-first, not sleep-first.** The loop runs immediately on `start()` (when not paused),
  then sleeps the interval, forever — deliberately NOT sleep-then-refresh. Reasoning: under
  launchd `KeepAlive` (PR #24, shipping alongside this) the daemon can restart far more often
  than weekly, and a sleep-first loop resets its wait on every restart, which could mean the
  graph plausibly never refreshes again — the exact failure this item exists to fix (nothing
  has refreshed the graph since 2026-07-12). Every write is an idempotent MERGE-upsert
  (`MemoryStore._upsert_node`), so re-running on a fast restart loop is wasted local time, not a
  correctness problem. A persisted last-refresh timestamp to avoid that waste is a separate,
  out-of-scope PR (see below).
- **The concurrency question, answered directly.** The daemon's own Memory handle is used by
  Brain from the event-loop thread, while the refresh writes from a worker thread
  (`asyncio.to_thread`, so the ~1.3-min write pass never blocks the loop). Kuzu's `Connection`
  is not documented safe for concurrent multi-thread access, so these two paths must never run
  at the same time against the same handle. **Chosen fix: skip the turn, not a cross-thread
  lock.** A `_graph_refresh_in_progress` flag is folded into the SAME `is_paused` predicate
  already wired into Brain (`self.state.is_paused or self.state.is_stopping or
  self._graph_refresh_in_progress`) — sets/clears only ever happen on the single event-loop
  thread (cooperative asyncio), so the flag itself is race-free, and while it's True Brain skips
  its whole pass (no recall, no model call, no remember — no reply at all, not even an apology).
  This was chosen over a `threading.Lock` spanning both the coroutine and the worker thread
  because: it reuses an existing, already-tested seam instead of a new primitive; it has the
  same worst-case latency cost as a lock would (a turn can wait out the ~1.3-min window either
  way); and it avoids a lock class of bugs (deadlock ordering, holding a lock across an `await`)
  for a once-a-week event. The honest cost: during that window the owner gets total silence,
  same as a manual pause — stated here, not hidden. `self.state.last_event` is set to
  `graph_refresh.started` / `.done` / `.failed` around the pass so Control API `status` at
  least shows it ran.
- **Shutdown correctness (found via review, fixed here, not deferred):** cancelling the
  refresh's `asyncio.Task` does NOT stop an already-running worker thread — `asyncio.to_thread`
  cannot interrupt work already executing in the thread pool, and this codebase's own voice-loop
  comment already documents that fact. The first cut of this change cancelled and moved straight
  to `compact()`/`close()`, which could close the Kuzu handle while the worker thread was still
  mid-`remember()`. Fixed with a `threading.Event` (`_graph_refresh_thread_idle`) that is set
  from INSIDE the worker thread's own `finally` (not the coroutine's), so it reflects true
  completion regardless of task cancellation; `stop()` now does a bounded join
  (`_GRAPH_REFRESH_SHUTDOWN_JOIN_TIMEOUT = 120s`, run via `asyncio.to_thread` so the wait itself
  doesn't block the loop) on that event BEFORE `compact()`/`close()` run. Covered by
  `test_stop_joins_an_in_flight_refresh_before_closing_memory`.
- **One exception must not permanently kill the weekly loop.** The refresh runs inside a thread
  wrapper that catches a declared, expected-failure tuple — `OSError` (e.g. `gh` missing),
  `subprocess.SubprocessError` (a `CalledProcessError` that survives `_gh_list_repos`'s own
  retries), `RuntimeError` (Kuzu query failures — verified empirically that `kuzu` raises plain
  `RuntimeError`), `sqlite3.Error` (sqlite-vec write failures) — and continues to the next
  scheduled cycle instead of letting the task die silently. Anything outside that declared set
  is a real bug and is allowed to surface. Covered by `test_graph_refresh_error_does_not_kill_the_loop`.
- **Known residual gap, stated rather than hidden:** the `is_paused` gate above only covers
  Brain's live-turn pass. Two OTHER call sites also write the same Memory handle from the loop
  thread without checking `is_paused` at all: `BackgroundReasoner`'s `on_done` callback
  (`brain/loop.py`, fires whenever a background opus job lands, at an arbitrary later time) and
  `finder/resolve.py`'s fire-and-forget `_deep_extract` (fired on a realtime resolve-on-miss).
  Both could theoretically still race a refresh's worker thread. Gating them is a separate,
  cross-cutting change (they're async-task callbacks, not a single call site) — flagged here for
  a follow-up, not built in this PR.
- **Explicitly out of scope (per the roadmap spec):** the `last_commit_date` incremental skip
  (a runtime optimisation, its own PR); deep-extract scheduling (Opus per repo, ~2600 calls);
  the missing SIGTERM handler; persisting a last-refresh timestamp (would avoid the
  refresh-first re-run cost on a fast restart loop, but needs its own design); gating the two
  residual writers noted above.
- **Files changed:** `edith/ingest/workspace.py` (injected `store` seam + `tenacity` retry on
  `_gh_list_repos`); `edith/daemon/edithd.py` (the scheduled refresh: constructor flags,
  `_start_graph_refresh` / `_make_default_graph_refresh` / `_wrap_graph_refresh_for_thread` /
  `_graph_refresh_loop`, the `is_paused` fold-in, `stop()`'s join); `tests/test_ingest_workspace.py`
  (+4 tests); `tests/test_daemon_edithd.py` (+7 tests).
- **Verification:** 359 tests green (348 baseline + 11 new), 0 failures; `ruff check edith
  tests` clean; `pyright edith tests` — identical 31 pre-existing errors before and after (all
  in files this PR did not touch: `voice/adapters.py`, `voice/live.py`, and typing noise in
  several test files) — zero new errors introduced. The daemon tests exercise real
  `threading.Event`-synchronized worker threads (not fixed sleeps) for the concurrency and
  shutdown-join assertions, run 5x locally with no flakiness observed.
- **What needs owner smoke (cannot be verified headlessly):** the REAL `ingest_workspace`
  passes against the live `~/.edith/data/memory.kuzu` (this PR never opens that path — all
  tests use `tmp_path`); whether a full weekly pass against 1378 real repos actually lands in
  ~1.3 min as measured, with the daemon's live Brain answering turns normally before/after;
  whether the owner notices/accepts the ~1.3-min silence window in practice.
