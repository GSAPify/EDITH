# EDITH — Build State

> Machine-and-human readable status. Update this at the end of every session (or at ~90% context).
> This is the first file a new session reads after `SESSION-PROTOCOL.md`.

**Current phase:** BUILDING → Slice 1 core COMPLETE (daemon shipped) + Memory Viewer shipped; ready for Slice 2
**Active slice:** 1 — Memory + Brain (all core components + edithd daemon shipped & validated)
**Last session:** 2026-07-07 — Session 6 (Memory graph viewer: `graph_snapshot()` + `edith/viewer/`, stdlib server + vendored force-graph, `--demo`; TDD RED-first, 70 tests green)

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
| — | Memory viewer | (07) | ✅ done | Offline local graph viewer: `MemoryStore.graph_snapshot()` + `edith/viewer/` (stdlib 127.0.0.1 server, vendored force-graph UMD, `--demo` seeder, `python -m edith.viewer`). **70 tests + 1 live-skipped, ruff/pyright clean.** Zero new runtime deps. Reads live Memory; repo ingestion (Slice 2) populates it for real. |

Legend: ⬜ not started · 🚧 in progress · ✅ done · ⏸ blocked

## Next action

**Slice 1 core is COMPLETE** (`edith/{memory,bus,router,brain,daemon}/` — 59 tests + 1
live-skipped, ruff/pyright clean, security/code/architecture reviewed). The daemon runs the full
recall→reason→remember loop under a unix-socket Control API.

**Session 6 shipped the Memory Viewer** (`docs/specs/07-memory-viewer.md`): `python -m
edith.viewer --demo` renders a dense force-directed cloud offline; live mode reads
`EDITH_DATA_DIR/memory.kuzu`. Schema gained `PR` + `authored_by`/`reviewed_by` (additive).

**Session 7 → start Slice 2 (PR-review skill)** — read `docs/specs/02-pr-review-skill.md`. It's
the first real autonomous action and exercises the Skill dispatch path end-to-end. Repo ingestion
there will populate the live graph the viewer already renders.

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
