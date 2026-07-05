# 04 — Session Awareness

> **Honest-framing reminder:** no unicorns. The event-collection layer for this slice is
> **unverified**. Candidate tap mechanisms exist and have been observed firing; whether any of
> them can reliably capture the exact input that drives the killer demo — owner-pasted terminal
> content — is **unknown until the spike runs.** Do not treat this spec as settled
> implementation. It is an interface contract around a component whose internals are
> TO PROTOTYPE.
>
> Architecture-level interfaces and cross-cutting rules are fixed in `00-north-star.md`.
> This file adds implementation depth for Slice 4 only — it does not restate what north-star
> already defines.

---

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
| **collector** | The unverified tap layer that reads raw OMC / Claude Code events from the machine. |
| **spike** | A time-boxed throwaway script to confirm what the collector can and cannot see. |

---

## Purpose

Slice 4 gives EDITH awareness of every running OMC / Claude Code session on the owner's
machine. SessionBus ingests raw activity from those terminals, normalises it into structured
events on the internal bus, and the Brain uses those events to narrate — by voice — what is
happening without the owner switching windows.

**The killer demo:** owner pastes an Airflow (or any) error into a terminal running OMC.
EDITH observes the paste and what OMC does next, then tells the owner — unprompted, by voice —
"Session 2 picked up your Airflow error; it's querying the DAG definition now." The owner
never leaves their current window.

Slice 4 ships that ambient narration. It does not ship the full Router (Slice 5), but calls
`model_call` through the existing single-tier passthrough as defined in north-star §7.

---

## Scope

**In:**
- The SessionBus collector: watching OMC / Claude Code terminals and emitting structured events.
- Event classification and narration policy (which events speak, which are silent).
- The `session.event` and `session.state` bus topic contracts.
- Voice narration of meaningful state changes via Brain → VoiceIO.
- Redaction of terminal content before any model call or bus publication.

**Out:**
- Full multi-tier model routing (Slice 5 — single-tier passthrough is acceptable here).
- Desktop terminal launching and control (Slice 6).
- Watching non-OMC processes (bare shell, arbitrary CLI tools).
- Any GUI for session status — menu-bar `status` command surfaces last event only.

---

## Interface to edithd

### SessionBus contract (north-star §4.3)

```
SessionBus: ingests OMC / Claude Code events
         → publishes session.event / session.state on the internal bus
```

Full multi-terminal → collector → bus → narration flow:

```
  Terminal 1 (OMC)          Terminal 2 (OMC)          Terminal 3 (OMC)
  ┌──────────────┐           ┌──────────────┐           ┌──────────────┐
  │  hooks? logs?│           │  hooks? logs?│           │  hooks? logs?│
  └──────┬───────┘           └──────┬───────┘           └──────┬───────┘
         │                          │                          │
         ▼                          ▼                          ▼
  ╔══════════════════════════════════════════════════════════════════════╗
  ║  COLLECTOR LAYER  ← ← ← TO PROTOTYPE / verify source first  ← ← ← ║
  ║  (mechanism unverified — see Build steps §1 and Open questions)    ║
  ╚══════════════════════════════════════════════════════════════════════╝
         │  raw events (Guard.redact applied here, before bus publish)
         ▼
  ┌──────────────────────────────────────────┐
  │  SessionBus  (edithd component)          │
  │  normalise → classify → gate → publish   │
  └──────────────────────┬───────────────────┘
                         │  session.event / session.state
                         ▼
  ┌──────────────────────────────────────────┐
  │  Internal event/message bus  (in-process)│
  └──────────────────────┬───────────────────┘
                         │
                         ▼
  ┌──────────────────────────────────────────┐
  │  BRAIN / ORCHESTRATOR                    │
  │  applies narration policy                │
  │  → model_call (budget-gated)             │
  └──────────────────────┬───────────────────┘
                         │  speak(text)
                         ▼
                    VoiceIO  →  🔊 owner hears narration
```

### Bus events (published by SessionBus)

Topic: **`session.event`**

Envelope: `{ topic, ts, source, payload }` — north-star §4.1.

Payload shape:
```json
{
  "session_id": "<omc-session-id>",
  "kind": "start | prompt | tool_use | error | stop",
  "summary": "<one-line human-readable, already redacted>",
  "repo": "<repo-name-if-detectable | null>"
}
```

Topic: **`session.state`**

Payload shape:
```json
{
  "session_id": "<omc-session-id>",
  "state": "working | waiting | error | idle",
  "current_action": "<one-line, redacted | null>",
  "repo": "<repo-name-if-detectable | null>"
}
```

`kind` and `state` are the classification the Brain uses for narration gating. Collector
internals (how `session_id` is discovered, how lines are parsed) are deferred to the spike.

### Inputs consumed

- **Raw terminal activity** — from the collector layer (mechanism TBD, see §Build steps 1).
- **`voice.utterance`** — Brain routes owner questions like "what is session 3 doing?" to
  SessionBus for a forced state summary.

### Outputs published

- `session.event` — discrete activity events per session.
- `session.state` — current state snapshot, refreshed on meaningful transitions.

### Control API surface

None new. The existing `status` command (north-star §4.2) returns `last_event`; SessionBus
populates that field. No new Control API commands in this slice.

---

## Data model

SessionBus is mostly stateless at the bus boundary — it emits and forgets. One lightweight
in-memory map is needed for state transitions:

```
session_states: dict[session_id, session.state payload]
```

Persisted to Memory (Slice 1) only for durable "what was session 2 doing at 11am" recall.
The graph node is: `(Session {id, repo, start_ts, last_state})`.

**Never persisted:** raw terminal lines, owner-pasted content, anything containing credentials.
Only normalised, already-redacted summaries may reach Memory — see Autonomy & secrets notes.

---

## Dependencies

- **Slice 1 (Memory + Brain):** Brain's decision loop must exist to consume `session.event`
  and trigger narration. Memory stores durable session summaries.
- **Slice 3 (Voice):** `speak(text)` must exist for narration output.
- **Slice 5 (Router):** SessionBus narration that needs a model call uses the single-tier
  passthrough (north-star §7: "Until then a single-tier passthrough is acceptable"). Full
  tiering arrives in Slice 5.
- **Guard (Slice 1):** `Guard.redact(payload)` must be callable before any bus publish or
  model call involving terminal content.

**Libraries (candidates — confirmed at spike time):**
- `watchdog` — filesystem event watcher for `.omc/` directory trees.
- `asyncio` + stdlib — tailing log/state files; no new async framework.
- `json` / `jsonlines` — parsing OMC state and hook payloads.

---

## Tech choices

Nothing deviates from north-star §5. Collector mechanism is the open question; no library
commit until the spike confirms what data is actually accessible.

---

## Autonomy & secrets notes

**Autonomy gate** (north-star §6.3):
- Narrating session activity is **AUTO** — it is a read-only observation of the owner's own
  machine and matches the §6.3 table entry "narrate."
- Summarising terminal content via a model call is **AUTO** when triggered by an observed
  event, subject to narration-policy gating below.
- Responding to an explicit owner question about a session ("what is it doing?") is **AUTO**.
- No write actions originate in this slice.

**Secrets** (north-star §6.1):

Terminal content is the highest-risk surface in the whole system. The killer-demo input — an
owner-pasted Airflow error — will typically contain a database connection URI with a password.
Transcript tails and hook payloads will routinely carry API keys, OAuth tokens, and `.env`
values.

Rules that apply here:

- `Guard.redact(payload)` runs on **every** raw collector line before it touches the bus,
  a model call, a log, or Memory. No raw terminal content is ever published unredacted.
- Redacted summaries only — never raw lines — may be stored in Memory or sent to Bifrost.
- If the collector tails `.omc/logs/` or session transcripts, those files may contain
  credentials from prior sessions. The ingestion pipeline must apply redaction on read,
  not after processing.
- SessionBus itself never writes to disk. Its in-memory `session_states` map holds only
  already-redacted summaries.

---

## Cost / token notes

Session awareness is the textbook always-on token-burner cited in north-star §6.2. Gate hard.

**Three narration classes:**

| Class | Trigger | Action | Model call? |
|-------|---------|--------|-------------|
| **Silent** | Routine tool calls (file reads, searches, incremental edits), rapid-fire PostToolUse noise, state transitions with no owner relevance | Logged to in-memory state only; bus publish but no speak | No |
| **Spoken locally** | Session start/stop, error detected, session now waiting on owner, long idle after active period | `speak(text)` with a locally-composed template string — no model | No |
| **Model-gated** | Owner explicitly asks "what is session X doing?"; genuine ambiguity needing summarisation (e.g. multi-step error cascade); a session that was error-state resolves — worth a one-liner | `model_call` via Router, budget-checked by Guard first | Yes — haiku default |

**Default is silent. Spoken is rare. Model calls are rarest.**

Budget impact: a well-tuned policy should produce O(1) model calls per notable session event,
not O(N) per tool call. Guard's per-window token budget (north-star §6.2) gates every
model-gated narration. The `haiku` tier is the default for narration; escalation to sonnet/opus
is not expected for this slice's output.

---

## Build steps (high-level, ordered)

### Step 0 — SPIKE: verify the event source (do this before everything else)

> **This is the highest-uncertainty piece in the entire plan (north-star §8). Do not proceed
> to step 1 until the spike produces a written finding.**

Write a throwaway script (`scratch/spike_session_tap.py`) that answers, on the actual machine:

1. **Candidate A — Claude Code hooks:** configure `SessionStart`, `PostToolUse`, `UserPromptSubmit`,
   and `Stop` hooks in a test session to POST JSON to a local netcat listener.
   - What does each hook's payload actually contain?
   - Does `UserPromptSubmit` fire when the owner pastes text into the OMC prompt?
   - Are hook payloads parseable and machine-stable across OMC versions?

2. **Candidate B — File tailing:** watch `.omc/logs/`, `.omc/state/sessions/{id}/`,
   `.omc/notepad.md`, and session transcripts for a running OMC session.
   - Which files update in near-real-time during a session?
   - Are they JSON/structured or free-form text?
   - What latency exists between an action and the file update?

3. **Critical sub-goal — paste capture:** manually paste an Airflow error into an OMC
   terminal. Does any tap — hook or file — record the pasted text? If neither does,
   note it explicitly; this blocks the killer demo.

Write findings in `scratch/spike_session_tap_findings.md`. That file decides the collector
mechanism for step 1.

### Step 1 — Implement the collector

Based on spike findings: implement the confirmed tap mechanism as the SessionBus collector.
If the spike finds a hybrid is needed (hooks for events + tailing for paste content), build
both — they are the collector's internals and hidden behind the `session.event` interface.

### Step 2 — Implement SessionBus normalisation and classification

Parse raw collector output → emit `session.event` and `session.state` payloads with the
shapes defined above. Apply `Guard.redact` before every publish.

### Step 3 — Implement narration policy in Brain

Brain subscribes to `session.event` and `session.state`. Apply the three-class policy table
above. Wire the locally-spoken class to `speak(text)` with template strings. Wire the
model-gated class to `model_call` behind Guard's budget check.

### Step 4 — Wire owner-question routing

Route `voice.utterance` events that ask about a session ("what's session 2 doing?") through
Brain → SessionBus → forced `session.state` publish → model-gated narration.

### Step 5 — Wire `status` command

Populate `last_event` in the Control API `status` response (north-star §4.2) from the
`session_states` in-memory map.

---

## Verification / testing

1. **Spike output (step 0):** `scratch/spike_session_tap_findings.md` exists and explicitly
   states which tap mechanism works and whether paste capture is possible.

2. **Unit: event normalisation.** Feed synthetic raw collector output through the normaliser;
   assert correct `kind`, `state`, `summary` shape; assert no credential strings survive
   (test with a payload containing a mock connection URI).

3. **Integration: end-to-end narration.** Run a real OMC session. Observe:
   - Start fires → spoken "Session started in [repo]" (locally composed, no model call).
   - A tool call fires → silent (no speak, no model call).
   - Session goes idle → spoken "Session 2 is waiting" (locally composed).
   - Ask EDITH verbally "what is session 2 doing?" → model-gated narration spoken within 3s.

4. **Redaction check.** Confirm via log inspection that no raw terminal line (pre-redact)
   appears in any bus event, model prompt, or Memory record.

5. **Budget check.** Run a 10-minute OMC session with normal activity. Inspect Guard's token
   usage counter. Model-call count should track model-gated events only (typically O(1–3)
   for the session, not O(N) tool calls).

---

## Open questions

- **Can any tap capture owner-pasted terminal content?** This is the #1 risk. If
  `UserPromptSubmit` does not fire for pastes, and file tailing misses them, the killer demo
  is not deliverable in v1. Resolved by spike step 0 sub-goal 3.

- **`UserPromptSubmit` hook availability.** This event type is not listed in the SESSION-PROTOCOL
  or example hook list for this project. It was observed firing in the current session
  (SubagentStart, PostToolUse). Does OMC expose `UserPromptSubmit` as a configurable hook?
  Resolved by spike candidate A.

- **File tail latency.** For real-time narration the file-tail path must surface events within
  ~1–2s of the action. Is `.omc/state/sessions/{id}/` written synchronously per action?
  Resolved by spike candidate B.

- **Session ID discovery.** How does SessionBus enumerate running OMC sessions? Via
  `.omc/state/sessions/` directory listing, or via hook registration at session start?
  Resolved by spike.

- **Terminal driver dependency.** Desktop control (Slice 6) will need a terminal driver
  (north-star §9, open question 2). Slice 4's collector may prototype that driver if file
  tailing requires watching terminal output directly. Track any terminal-driver learnings here
  for Slice 6.

- **Bare-shell sessions.** Owner sometimes runs commands directly in a shell, not through OMC.
  Those sessions produce no OMC hooks and no `.omc/` files. Scope them out for v1 or flag as
  a v2 extension?

---

## Completion Record — Session Awareness — (not yet built)

> Fill this at session end per `../SESSION-PROTOCOL.md` §4 (canonical template lives there).
> Leave empty until the slice is built.

- **What shipped:**
- **How it works:**
- **Key decisions made during build:**
- **Deviations from spec + why:**
- **Files created / changed:**
- **Verification / tests run + results:**
- **Follow-ups / known gaps:**
