# EDITH — Build State

> Machine-and-human readable status. Update this at the end of every session (or at ~90% context).
> This is the first file a new session reads after `SESSION-PROTOCOL.md`.

**Current phase:** SPEC COMPLETE → ready to build
**Active slice:** none — spec set finished; next session starts **Slice 1 build**
**Last session:** 2026-07-05 — Session 1 (brainstorm → full spec set)

## Slice status

| # | Slice | Spec | Build | Notes |
|---|-------|------|-------|-------|
| 0 | North-star architecture | ✅ done | — | Authoritative doc |
| 1 | Memory + Brain | ✅ done | ⬜ not started | Deep spec (555 ln). Storage = Kuzu native HNSW vector + sqlite-vec fallback |
| 2 | PR-review skill | ✅ done | ⬜ not started | Confirm-gate before GitHub review submit |
| 3 | Voice | ✅ done | ⬜ not started | ElevenLabs primary, local fallback; wake+STT local |
| 4 | Session awareness | ✅ done | ⬜ not started | Highest uncertainty — spec mandates a spike first |
| 5 | Router | ✅ done | ⬜ not started | Two-call latency masking (orchestration, not one inference) |
| 6 | Desktop control | ✅ done | ⬜ not started | Own-shell for OMC launches; Terminal.app osascript for visible term |

Legend: ⬜ not started · 🚧 in progress · ✅ done · ⏸ blocked

## Next action

**Session 2 → build Slice 1 (Memory + Brain).** Read `SESSION-PROTOCOL.md`, then this file, then
`docs/specs/01-memory-brain.md`. Before coding, get the Bifrost `base_url` + API key into `.env`
(needed for the Brain/Router). Set up uv + Python 3.11+ project scaffold as step 1.

## Blockers / needs from owner

- Bifrost `base_url` + API key (for Slice 1 Brain + Slice 5 Router).
- Decision to merge `spec/session-1-foundation` → establish `main` on GitHub.
