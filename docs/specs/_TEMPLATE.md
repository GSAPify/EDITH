# 0N — <Slice Name>

> **Honest-framing reminder:** no unicorns. "Unlimited context" = memory + retrieval +
> compaction; "two agents in one inference" = orchestration of two model calls (fast masks
> slow). If a section here implies a capability that doesn't exist, fix the section.
>
> This slice follows the shape below. Architecture-level interfaces + cross-cutting rules are
> fixed in `00-north-star.md` — **do not restate them, reference them.** This file adds
> *implementation* depth for this slice only.

## Terminology (glossary)

| Term | Meaning |
|------|---------|
| **EDITH** | The system. Always-on local-first macOS assistant. |
| **edithd** | The daemon process that runs everything under the hood. |
| **bus** | In-process event/message bus; components `publish`/`subscribe`. |
| **Guard** | Cross-cutting enforcement: `redact`, `authorize` (allow/ask/deny), budget. |
| **Router** | `model_call(messages, tier_hint) -> response` over the Bifrost adapter. |
| **Memory** | Graph + vector store: `recall` / `remember` / `compact`. |
| **SessionBus** | Watches OMC / Claude Code terminals → `session.event` / `session.state`. |
| **Skill** | Capability with `name`, `triggers`, `needs_confirmation`, `run(context)->result`. |
| **tier** | Model size class the Router selects: haiku / sonnet / opus. |

---

## Purpose

<One paragraph: what this slice is for and the usable thing it ships.>

## Scope

**In:** <what this slice does>
**Out:** <explicitly not in this slice — deferred or another slice's job>

## Interface to edithd

- **Inputs:** <events consumed / calls received>
- **Outputs:** <events published / return values>
- **Bus events:** <topics published & subscribed, with envelope shape>
- **Control contracts:** <any Control API surface this slice touches, if any>

## Data model (if any)

<Graph nodes/edges, vector records, on-disk schema. Omit if the slice is stateless.>

## Dependencies

- **Other slices:** <which slices must exist first, and what they provide>
- **Libraries:** <concrete libs, matching the north-star tech stack>

## Tech choices

<Slice-specific picks + one-line justification each. Defer to north-star §5 for stack-wide
choices; only note deviations or additions here.>

## Autonomy & secrets notes

- **Autonomy gate:** <which actions are AUTO vs. ASK for this slice — per north-star §6.3>
- **Secrets:** <what sensitive data this slice touches; how never-persist / redact-before-call
  / Keychain / encryption-at-rest apply here — per north-star §6.1>

## Cost / token notes

<Which events earn a model call vs. handled locally; expected tier(s); budget impact — per
north-star §6.2.>

## Build steps (high-level, ordered)

1. <step>
2. <step>
3. <…>

## Verification / testing

<How to prove the slice works: commands, expected output, manual checks. Fresh output at build
time, not assumptions.>

## Open questions

- <unresolved decision, with who/what resolves it>

---

## Completion Record — <slice> — <date>

> Fill this at session end per `../SESSION-PROTOCOL.md` §4 (canonical template lives there).
> Leave empty until the slice is built.

- **What shipped:**
- **How it works:**
- **Key decisions made during build:**
- **Deviations from spec + why:**
- **Files created / changed:**
- **Verification / tests run + results:**
- **Follow-ups / known gaps:**
