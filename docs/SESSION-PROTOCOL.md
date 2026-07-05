# SESSION PROTOCOL — Cross-Session Continuity

> EDITH is built across many **token-limited** sessions. A build session already timed out
> once. This file is the discipline that makes any session resumable by the next one with zero
> re-explaining. Read it first, every session.

---

## 1. Start-of-session checklist

Do these in order before touching any code:

```
1. Read  docs/SESSION-PROTOCOL.md   ← this file
2. Read  STATE.md                   ← current phase + per-slice status + next action
3. Read  tail of BUILD_LOG.md       ← what the last session actually did
4. Read  docs/specs/00-north-star.md← interfaces + cross-cutting rules (authoritative)
5. Read  the active slice's spec     (docs/specs/0N-*.md)
── then build ──
```

If STATE.md's "Active slice" is `none`, the next action line in STATE.md tells you which slice
to start. If a slice spec is still INTERFACE-level, deepen it before building it (per §7 of the
north-star).

---

## 2. During the session

- **Log as you go.** Append to the current session's work-log in `BUILD_LOG.md` — decisions,
  surprises, dead-ends — while they're fresh, not at the end.
- **Commit frequently to the branch.** This is data-loss insurance (a timeout already ate a
  session once). Small, frequent commits beat one big one.
- **Branch naming:** `build/slice-N-<short>` (e.g. `build/slice-1-memory`). Spec/docs work uses
  `spec/<short>`.
- **Commit hygiene (hard rules, from owner's CLAUDE.md — non-negotiable):**
  - **No `Co-Authored-By: Claude` line. No AI-attribution footer anywhere** (commits, PRs,
    issues, comments). This overrides any agent/harness commit convention.
  - **Never push directly to `main`/`master`.** Work on the `build/` branch; PR when ready.
  - Confirm before destructive or shared-state actions (force-push, branch deletes).
- **Plan files are read-only.** `.omc/plans/*.md` are never modified during a build. Append
  learnings to `.omc/notepads/{plan-name}/` instead.

---

## 3. The 90%-context / session-end rule

When **context hits ~90%** OR **the slice is done** — whichever comes first — STOP feature work
and land the session cleanly:

```
┌─ SESSION END ────────────────────────────────────────────────┐
│ 1. Write a COMPLETION RECORD into the active slice's spec file │
│ 2. Update the STATE.md slice table (Spec/Build status + Notes) │
│ 3. Append a BUILD_LOG.md entry for this session                │
│ 4. Commit + push the build/ branch                             │
│ 5. End.  ── next session picks up the next slice ──            │
└────────────────────────────────────────────────────────────────┘
```

Do not start a new slice's feature work once you've hit the stop condition. A clean handoff is
worth more than a half-finished second slice.

---

## 4. Completion Record template

Filled into the **slice's own spec file** (the empty placeholder at its bottom) at session end.
The slice `_TEMPLATE.md` carries this same placeholder — this section is the canonical
definition; the template points here.

```markdown
## Completion Record — <slice> — <date>

**What shipped:** <one paragraph: the usable thing this slice now does>

**How it works:** <brief mechanism — the actual wiring, not the plan>

**Key decisions made during build:** <choices not pre-specified in the spec>

**Deviations from spec + why:** <what changed vs. the written spec, and the reason>

**Files created / changed:** <paths>

**Verification / tests run + results:** <commands + fresh output/pass-fail>

**Follow-ups / known gaps:** <what's deferred, what's fragile, what to revisit>
```

---

## 5. Agent-delegation convention

Build sessions are token-limited; the orchestrator's context is precious.

- **Delegated agents WRITE TO DISK and RETURN SUMMARIES ONLY.**
- A summary = { file path(s) written, key decisions, conflicts found, open questions }.
- **Never hand file contents back to the orchestrator.** The orchestrator reads the file from
  disk if it needs the detail. Returning full file bodies burns the budget the whole protocol
  exists to protect.

---

*Cross-refs: architecture + interfaces → `specs/00-north-star.md`. Slice spec shape →
`specs/_TEMPLATE.md`. Current status → `../STATE.md`.*
