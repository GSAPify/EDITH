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
- Edges used: `owns` (unused this slice), `authored_by` (Repo→Person), `relates_to`
  (Fact→Repo, Fact→Person for the global owner-context facts).

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
  non-negotiable). `owns` edge unused this slice (no Project layer yet).
- **Files created / changed:** `edith/ingest/*` (new); `edith/memory/store.py`,
  `edith/memory/secrets.py` (extended); `tests/test_ingest_*.py`, `tests/test_secrets_filter.py`.
- **Verification:** 97 tests green (1 live-skipped), ruff/pyright clean, live smoke wrote 58
  nodes to a temp dir with a clean secret-scan.
- **Follow-ups / known gaps:** existing-DB migration (open question 1); Fact granularity;
  Skill wrapper for bus-triggered ingestion.
