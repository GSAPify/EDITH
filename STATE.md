# EDITH — Build State

> Machine-and-human readable status. Update this at the end of every session (or at ~90% context).
> This is the first file a new session reads after `SESSION-PROTOCOL.md`.

**Current phase:** BUILDING → Slice 1 core + Memory Viewer + Repo Ingestion + NL Finder shipped
**Active slice:** 1 — Memory + Brain (+ repo-ingestion pipeline + NL finder over the live graph)
**Last session:** 2026-07-07 — Session 9 (NL repo finder + real-time resolve-on-miss `edith/finder/`: `find_repos` model-free semantic+graph ranking, `summarize_hits` Sonnet, `python -m edith.finder`; `resolve_repo` hit / fast-Sonnet+background-Opus / not-found reusing ingest fetch/extract/graph_map + the redaction choke-point; thin Brain resolve-on-miss hook, default-off. Spec 09. 110 tests green, ruff/pyright clean. Live finder+ingest smoke on `agentsmith`)

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
| — | NL finder + resolve-on-miss | (09) | ✅ done | `edith/finder/`: `find_repos` (model-free semantic+graph fuse → `relates_to` walk → rank by strength+degree, degrades to graph-only when the live store has no vectors) + `summarize_hits` (Sonnet, injected); `python -m edith.finder "query"`. `resolve_repo` = HIT (graph `repo-<name>`) / RESOLVED (local clone or `gh` README → **REDACT choke-point** → fast Sonnet answer NOW + **background Opus** deep-extract coroutine the caller runs via `asyncio.create_task`, Slice-5 `think_async` seam) / NOT_FOUND (clean, no model). Thin Brain hook: recall-miss + repo mention + injected resolver → resolve then answer (**default `None` = no-op**, existing tests unchanged). Reuses ingest fetch/extract/graph_map. **110 tests + 1 live-skipped, ruff/pyright clean; planted-secret test proven non-vacuous.** Live smoke: `agentsmith` ingest (real Bifrost, relevance 0.72 Opus) → finder ranked it #1 with a real Sonnet summary; resolve HIT path no-model. |

Legend: ⬜ not started · 🚧 in progress · ✅ done · ⏸ blocked

## Next action

**Slice 1 core is COMPLETE** (`edith/{memory,bus,router,brain,daemon}/` — 59 tests + 1
live-skipped, ruff/pyright clean, security/code/architecture reviewed). The daemon runs the full
recall→reason→remember loop under a unix-socket Control API.

**Session 6 shipped the Memory Viewer** (`docs/specs/07-memory-viewer.md`): `python -m
edith.viewer --demo` renders a dense force-directed cloud offline; live mode reads
`EDITH_DATA_DIR/memory.kuzu`. Schema gained `PR` + `authored_by`/`reviewed_by` (additive).

**Session 8 shipped Repo Ingestion** (`docs/specs/08-repo-ingest.md`): `edith/ingest/` feeds the
live graph the viewer renders. **Orchestrator next:** review, then trigger the full
contributed-repos run — `python -m edith.ingest` (env BIFROST_*), no `--repos` cap, into the real
`EDITH_DATA_DIR`. NOTE the schema-migration open question if a live `memory.kuzu` already exists
(none does yet — fresh creation is clean). While there, the `secrets.py` markdown-wrapper fix
(Session 8) also hardens Brain/remember for every future ingest.

**Then → start Slice 2 (PR-review skill)** — read `docs/specs/02-pr-review-skill.md`. First real
autonomous action; exercises the Skill dispatch path end-to-end.

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
- Decision to merge `spec/session-1-foundation` → establish `main` on GitHub.
