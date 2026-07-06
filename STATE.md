# EDITH — Build State

> Machine-and-human readable status. Update this at the end of every session (or at ~90% context).
> This is the first file a new session reads after `SESSION-PROTOCOL.md`.

**Current phase:** BUILDING → Slice 1 (Memory + Brain)
**Active slice:** 1 — Memory + Brain (Memory *store* foundation shipped; Brain/daemon next)
**Last session:** 2026-07-06 — Session 3 (Slice 1 vector layer → sqlite-vec, strict TDD)

## Slice status

| # | Slice | Spec | Build | Notes |
|---|-------|------|-------|-------|
| 0 | North-star architecture | ✅ done | — | Authoritative doc |
| 1 | Memory + Brain | ✅ done | 🚧 in progress | Memory store green (graph+vector recall, secrets filter, 13 tests). Vector layer now **sqlite-vec** (Kuzu keeps graph) — incremental inserts, fact-after-index recalled immediately. Next: Brain loop + edithd. |
| 2 | PR-review skill | ✅ done | ⬜ not started | Confirm-gate before GitHub review submit |
| 3 | Voice | ✅ done | ⬜ not started | ElevenLabs primary, local fallback; wake+STT local |
| 4 | Session awareness | ✅ done | ⬜ not started | Highest uncertainty — spec mandates a spike first |
| 5 | Router | ✅ done | ⬜ not started | Two-call latency masking (orchestration, not one inference) |
| 6 | Desktop control | ✅ done | ⬜ not started | Own-shell for OMC launches; Terminal.app osascript for visible term |

Legend: ⬜ not started · 🚧 in progress · ✅ done · ⏸ blocked

## Next action

**Session 4 → continue Slice 1.** Memory *store* is green (`edith/memory/`, 13 tests);
vector layer is now sqlite-vec (incremental, no rebuild — the re-index blocker is resolved).
Next concrete step: build the **Brain loop skeleton** (bus subscriptions +
recall→assemble→decide→remember pass with a single-tier `Router.model_call`
passthrough), then `edithd` lifecycle + Control API. `compact()` needs
Session/Conversation node tables + a working-context buffer first — add those,
then implement compaction. Before Brain/Router: Bifrost `base_url` + key.

## Blockers / needs from owner

- ~~**Vector re-index decision (from Session 2).**~~ **RESOLVED + IMPLEMENTED (Session 3).**
  Adopted option (b): the vector layer is now **sqlite-vec** (Kuzu keeps the graph). Inserts
  are incremental — a Fact remembered after the store exists is recalled immediately, no
  rebuild. north-star Open Question #1's maturity caveat is now resolved with a working impl.
- Bifrost `base_url` + API key (for Slice 1 Brain + Slice 5 Router).
- Decision to merge `spec/session-1-foundation` → establish `main` on GitHub.
