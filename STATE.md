# EDITH — Build State

> Machine-and-human readable status. Update this at the end of every session (or at ~90% context).
> This is the first file a new session reads after `SESSION-PROTOCOL.md`.

**Current phase:** BUILDING → Slice 1 (Memory + Brain)
**Active slice:** 1 — Memory + Brain (Memory store + bus + Router + Brain loop shipped; edithd daemon next)
**Last session:** 2026-07-06 — Session 4 (Slice 1 bus + Router/Bifrost adapter + Brain loop, strict TDD)

## Slice status

| # | Slice | Spec | Build | Notes |
|---|-------|------|-------|-------|
| 0 | North-star architecture | ✅ done | — | Authoritative doc |
| 1 | Memory + Brain | ✅ done | 🚧 in progress | Memory store + **bus** (async pub/sub) + **Router** (Bifrost adapter, tenacity retries, live-smoke green) + **Brain loop** (recall→assemble→redact→model_call→remember→publish, passthrough) all green (29 tests + 1 live-skipped). Next: **edithd daemon + Control API**; then `compact()` + Guard. |
| 2 | PR-review skill | ✅ done | ⬜ not started | Confirm-gate before GitHub review submit |
| 3 | Voice | ✅ done | ⬜ not started | ElevenLabs primary, local fallback; wake+STT local |
| 4 | Session awareness | ✅ done | ⬜ not started | Highest uncertainty — spec mandates a spike first |
| 5 | Router | ✅ done | ⬜ not started | Two-call latency masking (orchestration, not one inference) |
| 6 | Desktop control | ✅ done | ⬜ not started | Own-shell for OMC launches; Terminal.app osascript for visible term |

Legend: ⬜ not started · 🚧 in progress · ✅ done · ⏸ blocked

## Next action

**Session 5 → continue Slice 1: `edithd` daemon lifecycle + Control API.** Bus, Router
(Bifrost adapter, live-smoke-verified) and the Brain loop passthrough are all green
(`edith/bus/`, `edith/router/`, `edith/brain/` — 29 tests + 1 live-skipped). Next concrete
step: build **`edithd`** — process bring-up (Keychain secrets → mount encrypted volume → open
Kuzu → bus → subsystems → Control API server), the unix-socket Control API
(`pause`/`resume`/`kill`/`status`, locked shape), the pause-suspends-Memory decision, and the
launchd plist. Then `compact()` (needs Session/Conversation node tables + a token-counted
working-context buffer) and **Guard** (`redact`/`authorize`/budget — Brain currently redacts
inline as the interim). Router two-call masking / streaming / tier heuristics = Slice 5.

## Blockers / needs from owner

- ~~**Vector re-index decision (from Session 2).**~~ **RESOLVED + IMPLEMENTED (Session 3).**
  Adopted option (b): the vector layer is now **sqlite-vec** (Kuzu keeps the graph). Inserts
  are incremental — a Fact remembered after the store exists is recalled immediately, no
  rebuild. north-star Open Question #1's maturity caveat is now resolved with a working impl.
- ~~Bifrost `base_url` + API key (for Slice 1 Brain + Slice 5 Router).~~ **RESOLVED (Session 4):**
  in the gitignored `.env`; Router live smoke hit real Bifrost (200, non-empty). Key was pasted
  in chat 2026-07-06 → **rotate it in Bifrost** (noted in `.env`). Keychain retrieval = daemon work.
- Decision to merge `spec/session-1-foundation` → establish `main` on GitHub.
