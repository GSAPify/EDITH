# 09 — NL Repo Finder + Real-time Resolve-on-miss

> Architecture-level interfaces + cross-cutting rules are fixed in `00-north-star.md`
> and reused here (Memory `recall`/`remember`/`semantic_recall`, secrets `sanitize_text`,
> Router `model_call`, the ingest `extract`/`graph_map` path). This file adds implementation
> depth for the finder slice only.

## Terminology (glossary)

See `_TEMPLATE.md`. Slice-specific: **resolve-on-miss** = fetching + answering about a repo the
owner asks about that is NOT yet in the graph; **fast path** = the immediate Sonnet answer;
**background** = the deferred Opus deep-extract that makes the NEXT mention a graph hit.

---

## Purpose

Give EDITH two abilities over the ingested Memory graph (spec 08):

1. **NL repo finder** — `find_repos(query, store, k)` answers "which of my repos does X?" by
   fusing semantic + graph search and ranking the owner's repos. A CLI (`python -m edith.finder
   "…"`) prints the ranking and, with Bifrost env present, a one-line Sonnet summary.
2. **Real-time resolve-on-miss** — the owner's key requirement. When EDITH is asked about a repo
   NOT in the graph, `resolve_repo` locates it (local clone or `gh`), redacts, answers FAST with
   Sonnet now, and schedules a BACKGROUND Opus deep-extract → `remember` so the next mention is an
   instant graph hit. A thin Brain hook wires this before answering.

## Scope

**In:** `find_repos` (model-free ranking) + `summarize_hits` (injected Router, Sonnet);
`resolve_repo` (hit / resolved / not-found) with the fetch → REDACT → fast-answer → background
split; the `python -m edith.finder` CLI; a thin Brain resolve-on-miss hook (injected, default-off).

**Out:** the Slice-5 `think_async` formalization of the background job (this slice exposes the
deep-extract as an awaitable the caller runs via `asyncio.create_task`); a Skill wrapper; changing
ingest to write vectors (see Open questions); voice/TTS delivery (Slice 3).

## Interface to edithd

- **Inputs:** `find_repos(query, store, k) -> list[RepoHit]`;
  `resolve_repo(name, store, router, *, scan_root, gh_readme, deep_max_tokens) -> ResolveResult`.
- **Outputs:** ranked `RepoHit`s; a `ResolveResult` (status + fast answer + a background coroutine).
- **Bus events:** none of its own. Brain's existing `voice.utterance` → `brain.decision` loop gains
  the resolve-on-miss step (spec 01 §core loop, step 1b).
- **Control contracts:** none.

## Data model

Stateless for `find_repos`. `resolve_repo` WRITES via the existing ingest schema
(`Repo`/`Person`/`Project`/`Fact` + `relates_to`/`authored_by`/`owns`, spec 08) using
`graph_map.map_and_remember` — no new node or edge types. `RepoHit` and `ResolveResult` are
in-memory dataclasses.

## Tech choices

- **Ranking is model-free (deterministic).** `find_repos` fuses `store.semantic_recall` (sqlite-vec
  KNN over Fact embeddings) AND `store.recall` (substring + 1-hop graph), walks `relates_to` to Repo
  nodes via `graph_snapshot` (which already carries links + per-node degree), and ranks by match
  strength + a small degree nudge. The Sonnet summary is a separate, injected path that never
  affects the ranking — tested with a fake Router, no live call.
- **Graceful degradation to graph-only.** Ingest currently writes a plain `MemoryStore` (no vector
  sibling), so the live graph has NO Fact embeddings; the semantic signal is empty there and the
  substring/graph signal carries the result. `find_repos` accepts either a `MemoryStore` or a
  `VectorMemoryStore` and uses the vector signal only when present (an `isinstance` guard).
- **Resolver is fully injected.** `resolve_repo` takes the store, a `model_call`-shaped router, a
  `scan_root`, and a `gh_readme` callable, so unit tests use deterministic doubles — a fake local
  clone dir and a fake gh fetcher — with no network and no live model.
- **Fast/background split.** The fast Sonnet answer returns immediately; the Opus deep-extract is a
  coroutine on the `ResolveResult` that the caller schedules with `asyncio.create_task`. The live
  turn is NEVER blocked on Opus (north-star §6.2, spec 05 §Background reasoning).

## Autonomy & secrets notes

- **Autonomy gate:** AUTO — read-only over the owner's own files + repo docs, plus model calls and
  a write to the owner's own Memory. No external side effects, no shared-state writes (north-star
  §6.3).
- **Secrets (redaction-first, north-star §6.1):** the unbypassable choke-point is `redact_docs`
  (`sanitize_text` over every fetched field), run ONCE at the fetch boundary so BOTH the fast answer
  and the background extract inherit clean docs. Defence-in-depth: `_fast_answer` re-sanitizes the
  message content at the model egress, `extract._call` re-sanitizes at the ingest model egress, and
  `remember`/`sanitize_node` re-sanitizes at the graph egress. A planted-secret test asserts the
  secret reaches NEITHER the fake Router NOR the store on the fast path or the background path (and
  was verified non-vacuous: disabling the redaction layers makes it fail). `summarize_hits`
  redacts the candidate blob too.

## Cost / token notes

- `find_repos` ranking: **zero model calls** — pure graph/vector reads.
- `summarize_hits`: one **Sonnet** call over the top hits (latency-first voice); skipped entirely
  when there are no hits.
- `resolve_repo` miss: one **Sonnet** fast answer + one budget-aware **Opus** deep-extract in the
  background (only when Sonnet-relevance ≥ threshold, inherited from `extract_repo`). A HIT and a
  NOT_FOUND make **zero** model calls.

## Build steps (high-level, ordered)

1. `finder.py` — `find_repos` (semantic+graph fuse → Repo walk → rank) + `summarize_hits`.
2. `resolve.py` — `resolve_repo`: hit-check (`repo-<name>`), locate (local clone / `gh` README),
   redact, fast Sonnet answer, background Opus `_deep_extract` reusing `extract_repo`+`map_and_remember`.
3. `__main__.py` — the `python -m edith.finder "…"` CLI (ranking always; Sonnet summary if Bifrost
   env present), mirroring `edith/ingest/__main__.py`.
4. `edith/brain/loop.py` — thin resolve-on-miss hook: on a recall MISS + a repo mention + an injected
   resolver, resolve then fold the fast answer into the working context; schedule the background job.

## Verification / testing

- `uv run pytest` — 110 tests (1 live-skipped). New: finder ranking (relevant repo ranked first,
  model-free; `k` respected; graph-only fallback; Sonnet summary via fake Router; empty → no call);
  resolve (hit returns from memory with no fetch/model; local-clone miss → fast answer + scheduled
  background; gh-README miss; not-found clean; **planted-secret never reaches Router or store**);
  Brain hook (miss invokes resolver + answers; recall-hit skips it; default no-resolver unchanged).
- `ruff check edith tests` + `pyright edith` — clean.
- Live smoke: `python -m edith.finder "<query>" --data-dir <temp>` after a tiny `run_ingest` into a
  temp dir (1 repo, real gh/Bifrost, small `--max-tokens`) — prints the ranking (+ Sonnet summary),
  redacted.

## The decisions (chosen defaults)

1. **Ranking signal** → fuse semantic (`semantic_recall`) + graph (`recall`), walk `relates_to` to
   Repos, rank by match strength + `0.1 × degree`. Model-free.
2. **Degrade to graph-only** when the store carries no vectors (the current live case).
3. **Resolve locate order** → local `~/gitstuff/<name>` patterninc clone first (offline, ground
   truth, spec 08), then a best-effort `gh api repos/patterninc/<name>/readme`.
4. **Redaction point** → once at the fetch boundary (`redact_docs`), so both branches inherit it;
   plus defence-in-depth at every model and graph egress.
5. **Background seam** → the deep-extract is a coroutine on `ResolveResult`; the caller runs it via
   `asyncio.create_task`. Slice-5 `think_async` (spec 05 §Background reasoning) will formalize this
   into a real job handle with `id`/`status`/`cancel`.
6. **Brain hook** → injected resolver, default `None` (no-op); fires only on a recall miss with a
   repo mention. Repo-name extraction is a thin `<name> repo` heuristic, NOT an NLP layer.

## Open questions

- **Ingest writes a plain `MemoryStore`, not `VectorMemoryStore`** (`ingest/pipeline.py`), so the
  live graph has no Fact embeddings and the finder's semantic signal is empty on live data — the
  graph signal carries it. Switching ingest to `VectorMemoryStore` would light up the semantic path
  for the finder too. Filed here; NOT changed in this slice (out of scope — one root cause per PR).
  The resolve background path DOES write via whatever store it is handed, so passing a
  `VectorMemoryStore` there makes resolved repos semantically searchable immediately.
- **Repo-name extraction** is intentionally a thin heuristic. A future Slice can replace it with a
  Sonnet intent classifier if the `<name> repo` phrasing proves too narrow.

---

## Completion Record — 09 nl-finder — 2026-07-07

- **What shipped:** `edith/finder/` (`finder.py`, `resolve.py`, `__main__.py`, `__init__.py`) + a
  thin Brain resolve-on-miss hook + this spec. NL repo finder (model-free semantic+graph ranking,
  optional Sonnet summary) and real-time resolve-on-miss (hit / fast-Sonnet + background-Opus /
  not-found) reusing the ingest fetch/extract/graph_map + secrets choke-point.
- **How it works:** `find_repos` fuses `semantic_recall`+`recall`, walks `relates_to` to Repos via
  `graph_snapshot`, ranks by strength+degree. `resolve_repo` checks the graph (`repo-<name>`), else
  locates via local clone / `gh` README, REDACTs at the fetch boundary, answers fast with Sonnet,
  and returns a background Opus deep-extract coroutine the caller schedules. Brain fires it only on
  a recall miss + repo mention when a resolver is injected.
- **Key decisions:** the six above.
- **Deviations from spec:** none material. `think_async` (Slice 5) is a documented seam, as required.
- **Files created / changed:** `edith/finder/*` (new); `edith/brain/loop.py` (thin hook); tests
  `tests/test_finder.py`, `tests/test_finder_resolve.py`, `tests/test_brain_resolve_hook.py` (new).
- **Verification:** 110 tests green (1 live-skipped), ruff/pyright clean, planted-secret test proven
  non-vacuous, live finder smoke against a temp-ingested store.
- **Follow-ups / known gaps:** ingest → `VectorMemoryStore` so the live semantic path lights up
  (open question 1); Skill wrapper for bus-triggered finding; richer repo-name intent detection.
