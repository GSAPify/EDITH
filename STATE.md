# EDITH — Build State

> Machine-and-human readable status. Update this at the end of every session (or at ~90% context).
> This is the first file a new session reads after `SESSION-PROTOCOL.md`.

**Current phase:** Slice 1 + Viewer + Ingest + NL Finder DONE & consolidated. Ready for Slice 2.
**Active slice:** → **Slice 2 (PR-review skill)** on branch `build/slice-2-pr-review` (cut off master).
**Repo:** everything is on **`master`** (renamed from `main`, Session 11). All feature branches merged
+ deleted; `master` is the GitHub default. New work = branch off `master`, PR in. Full graph LIVE in
`~/.edith/data/memory.kuzu` (206 nodes: 23 Repo, 26 Person, 12 Project, 145 Fact — embedded, no leak).
**Prev session:** 2026-07-07 — Session 10 (closed the ingest↔finder embedding gap, TDD). Ingest now writes via `VectorMemoryStore` so Facts are embedded on `remember` (Fix 1, `run_ingest(embedder=…)` seam); `VectorMemoryStore.backfill_embeddings()` + `python -m edith.ingest --reembed` embed existing graph-only Facts with the LOCAL embedder, no model calls, idempotent, credential-free (Fix 2); `find_repos` adds a per-token graph fallback that fires ONLY when both signals score zero, so a populated graph never silently returns nothing (Fix 3). 114 tests green, ruff/pyright clean. Live: `--reembed` embedded the 145 real Facts; `python -m edith.finder "seo tools"` now returns real repos (was "No repos matched"). Known limitation documented: Kuzu embedded is single-process (lock contention across viewer/finder/ingest; prod fix = route all DB access through `edithd`).

## Slice status

| # | Slice | Spec | Build | Notes |
|---|-------|------|-------|-------|
| 0 | North-star architecture | ✅ done | — | Authoritative doc |
| 1 | Memory + Brain | ✅ done | ✅ core done | Memory (Kuzu graph + sqlite-vec) + **bus** + **Router** (Bifrost, live-smoke green) + **Brain loop** + **edithd daemon** (unix-socket Control API pause/resume/kill/status, 0600, startup/shutdown ordering, pause-suspends-Memory, launchd plist template) — **59 tests + 1 live-skipped, ruff/pyright clean, 3-reviewer validated**. Documented seams left for their slices: `compact()`, Guard (budget/authorize), encrypted-volume mount, VoiceIO/SessionBus wiring. |
| 2 | PR-review skill | ✅ done | ⬜ not started | Confirm-gate before GitHub review submit |
| 3 | Voice | ✅ done | ⬜ not started | ElevenLabs primary, local fallback; wake+STT local |
| 4 | Session awareness | ✅ done | ⬜ not started | Highest uncertainty — spec mandates a spike first |
| 5 | Router | ✅ done | ⬜ not started | Two-call latency masking (orchestration, not one inference) |
| 6 | Desktop control | ✅ done | ⬜ not started | Own-shell for OMC launches; Terminal.app osascript for visible term |
| — | Memory viewer | (07) | ✅ done | Offline local graph viewer: `MemoryStore.graph_snapshot()` + `edith/viewer/` (stdlib 127.0.0.1 server, vendored force-graph UMD, `--demo` seeder, `python -m edith.viewer`). **70 tests + 1 live-skipped, ruff/pyright clean.** Zero new runtime deps. Reads live Memory; repo ingestion populates it for real. |
| — | Repo ingestion | (08) | ✅ done | `edith/ingest/` populates the LIVE graph from local `patterninc` clones: discover→fetch→**REDACT (choke-point)**→Sonnet classify/Opus deep→map→`remember`. `python -m edith.ingest [--dry-run] [--repos] [--limit] [--data-dir] [--max-tokens]`, incremental skip on `Repo.last_commit_date`, secret-safe status report, one-time global `~/.claude/CLAUDE.md` owner context. Additive schema (`Repo` +4 cols, `Fact.source`, `authored_by` Repo→Person). **97 tests + 1 live-skipped, ruff/pyright clean.** Live smoke: 58 nodes to a temp dir, secret-scan clean. Full contributed-repos run is orchestrator-gated pending review. |
| — | NL finder + resolve-on-miss | (09) | ✅ done | `edith/finder/`: `find_repos` (model-free semantic+graph fuse → `relates_to` walk → rank by strength+degree; **Session 10:** per-token graph fallback fires when both signals score zero so a populated graph never silently returns nothing) + `summarize_hits` (Sonnet, injected); `python -m edith.finder "query"`. **Session 10:** ingest now writes via `VectorMemoryStore` so live Facts ARE embedded; `python -m edith.ingest --reembed` backfills existing graph-only Facts (local embedder, no model cost, idempotent). Live: 145 Facts reembedded, `finder "seo tools"` returns real repos. `resolve_repo` = HIT (graph `repo-<name>`) / RESOLVED (local clone or `gh` README → **REDACT choke-point** → fast Sonnet answer NOW + **background Opus** deep-extract coroutine the caller runs via `asyncio.create_task`, Slice-5 `think_async` seam) / NOT_FOUND (clean, no model). Thin Brain hook: recall-miss + repo mention + injected resolver → resolve then answer (**default `None` = no-op**, existing tests unchanged). Reuses ingest fetch/extract/graph_map. **110 tests + 1 live-skipped, ruff/pyright clean; planted-secret test proven non-vacuous.** Live smoke: `agentsmith` ingest (real Bifrost, relevance 0.72 Opus) → finder ranked it #1 with a real Sonnet summary; resolve HIT path no-model. |

Legend: ⬜ not started · 🚧 in progress · ✅ done · ⏸ blocked

## Next action

### ▶ SLICE 2 — PR-review skill (START HERE)
Branch `build/slice-2-pr-review` is already cut off `master`. Read `docs/specs/02-pr-review-skill.md`.
This is the first real autonomous action; it exercises the Skill dispatch path end-to-end AND is the
payoff of Slices 1/ingest/finder — the memory graph now has real people + repos to resolve against.

**Canonical flow:** "EDITH, review Tavishi's PR" → resolve the *person* + *repo* (REUSE
`edith/finder` `find_repos`/`resolve_repo`; 26 Person + 23 Repo nodes exist in the live graph) →
find the PR via `gh` → fetch the diff → review it (route deep review to **Opus**) → **ASK before
posting anything to GitHub** (the review submit is a shared-state write → `Skill.needs_confirmation
= True`, the confirm-gate is the crux) → post/summarize. **Ask-when-unsure**: if person/repo/PR
can't be resolved, ASK rather than guess (owner requirement).

**Reuse, don't rebuild:** `edith/router` (Opus review / Sonnet search), `edith/memory` (people/repos),
`edith/brain` (Skill dispatch + the Skill contract: `name`/`triggers`/`needs_confirmation`/`run`),
`edith/ingest` (repo context), `edith/finder` (resolve people/repos). `gh` is authed as **GSAPify**
(scopes `repo`, `read:org`) — can read PRs and post reviews.

**Gotchas the orchestrator MUST heed (learned this build):**
- Delegated agents return a terse "Complete." — **verify independently + do a LIVE run.** Tests
  green ≠ works (the ingest→finder embed bug passed 110 tests but returned nothing live).
- **Kuzu is single-process** → stop the viewer (`lsof -ti tcp:8765 | xargs kill`) before running
  anything that opens `memory.kuzu`.
- Bifrost creds are in gitignored `.env` (source it: `set -a; source .env; set +a`). **KEY STILL
  NEEDS ROTATING** (pasted in chat 2026-07-06).
- The owner's Pattern commit identity ≠ GSAPify (author filters unreliable) → resolve people via
  the graph + `gh pr list`, not `--author=GSAPify`.
- Confirm-gate before ANY `gh` write (posting a review). Live smoke: read a real PR (read-only) to
  prove the fetch+review path; never auto-submit.

**Deferred Slice-1 seams** (pick up when their slice needs them, not blocking Slice 2):
`compact()` (needs Session/Conversation node tables + token-counted working buffer); **Guard**
(`authorize`/budget — Brain redacts inline as the interim; `budget_used=0` stub in Control API);
encrypted-volume mount (LocalSecureStore enforces 0700 dev dir); VoiceIO/SessionBus wiring
(Brain already subscribes to their bus topics). Router two-call masking / `think_async` /
tier heuristics = Slice 5.

## Blockers / needs from owner

- ~~**Vector re-index decision (from Session 2).**~~ **RESOLVED + IMPLEMENTED (Session 3).**
  Adopted option (b): the vector layer is now **sqlite-vec** (Kuzu keeps the graph). Inserts
  are incremental — a Fact remembered after the store exists is recalled immediately, no
  rebuild. north-star Open Question #1's maturity caveat is now resolved with a working impl.
- ~~Bifrost `base_url` + API key (for Slice 1 Brain + Slice 5 Router).~~ **RESOLVED (Session 4):**
  in the gitignored `.env`; Router live smoke hit real Bifrost (200, non-empty). Key was pasted
  in chat 2026-07-06 → **rotate it in Bifrost** (noted in `.env`). Keychain retrieval = daemon work.
- ~~Establish `main` on GitHub.~~ **DONE (Session 11):** all branches consolidated into **`master`**
  (renamed from `main`), set as default, redundant branches deleted. Single-branch repo now.

## Known limitations

- **Kuzu embedded is single-process (Session 10).** The viewer, finder, and ingest each open
  `memory.kuzu` directly and contend on the on-disk file lock — only ONE may hold it at a time
  (running `--reembed` requires no other EDITH process on the DB). The production fix is routing
  ALL DB access through `edithd` (one owner of the handle; every other surface talks over the
  Control API). Noted, not built — out of scope for the embedding-gap fix.
