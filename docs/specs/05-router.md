# 05 — Router

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
| **Bifrost** | Pattern's Anthropic/OpenAI-compatible model gateway (provider-agnostic). |
| **latency masking** | Firing a fast model for an immediate ack while a slow model reasons in parallel. Two separate calls. Not one inference. |

---

## Purpose

Router is EDITH's model-call gateway. It selects the cheapest model tier that can do the job
(haiku / sonnet / opus), calls the Bifrost adapter, and owns the two-call latency-masking
mechanics — firing a fast haiku acknowledgement and a slow opus answer as two overlapping
(but fully separate) calls so that TTS audio starts before the slow call finishes. Every model
call in EDITH routes through this contract. Until this slice ships, callers use a single-tier
passthrough behind the same contract; slice 5 replaces the internals without changing the
surface.

## Scope

**In:**
- Tier selection logic (haiku / sonnet / opus) from `tier_hint` + internal heuristics.
- Bifrost adapter (provider-agnostic; base_url + tier→model map from config).
- Two-call latency-masking mechanics (haiku ack + opus answer, overlapped).
- Streaming: partial token delivery to callers (VoiceIO / TTS can start before completion).
- Consulting Guard's budget check before escalating to opus.
- Redacting messages via `Guard.redact()` as the first step inside every `model_call`.

**Out:**
- Whether an event *earns* a model call at all — that's Brain + Guard's budget gate
  (north-star §6.2). Router is called only after Brain/Guard have already approved a call.
- Budget accounting and tracking — Guard owns that state.
- Autonomy gate decisions — Guard's `authorize()` contract.
- TTS synthesis — VoiceIO / Slice 3.
- When to apply the two-call pattern — Brain decides to invoke it; Router provides the
  mechanics.

---

## Interface to edithd

- **Inputs:** `model_call(messages, tier_hint) -> response` — called directly by Brain (in-process).
  Also a streaming variant: `model_call_stream(messages, tier_hint) -> AsyncIterator[chunk]`.
- **Outputs:** completed response object (non-streaming) or async token stream (streaming).
- **Bus events:** Router publishes no events of its own and subscribes to none. It is
  invoked synchronously (or via `await`) from Brain. Any cost telemetry it surfaces is passed
  back to Guard as a return annotation; Guard owns the `budget.warning` topic.
- **Control contracts:** none — Router has no Control API surface.

### Contract signatures (Python, interface-level)

```python
# Single-response call
async def model_call(
    messages: list[dict],
    tier_hint: Tier,            # HAIKU | SONNET | OPUS
) -> ModelResponse: ...

# Streaming call — yields token chunks as they arrive
async def model_call_stream(
    messages: list[dict],
    tier_hint: Tier,
) -> AsyncIterator[ModelChunk]: ...

# Two-call latency-masking pattern — called by Brain when it wants an ack+answer pair
async def model_call_masked(
    messages: list[dict],
    ack_prompt: str,            # short prompt that produces the spoken filler
) -> tuple[AsyncIterator[ModelChunk], asyncio.Task[ModelResponse]]:
    # Returns (haiku_ack_stream, opus_answer_task) — both already started
    ...

# Supervised reasoning — opus runs cancellable while a haiku narrator describes its
# live progress and a sonnet arbiter can stop/redirect it on an owner interjection.
async def supervised_reason(
    messages: list[dict],
    on_narration: Callable[[str], Awaitable[None]],  # haiku's live "what opus is doing" → VoiceIO.speak()
) -> "SupervisedSession": ...

class SteeringDecision(Enum):
    CONTINUE = "continue"     # ignore the interjection, opus keeps going
    STOP = "stop"             # cancel opus, end the turn
    REDIRECT = "redirect"     # cancel opus, relaunch with the correction + kept partial context

class SupervisedSession:
    result: asyncio.Task[ModelResponse]                            # the opus task (cancellable)
    async def steer(self, utterance: str) -> SteeringDecision: ... # sonnet arbiter reads utterance + opus state
    async def stop(self, keep_partial: bool = True) -> None: ...   # cancel opus; keep partial reasoning as context by default
```

---

## Tier selection

### Routing philosophy — latency-first (revised Session 3)

EDITH's *talking voice* is **Sonnet**, not Opus. Rationale: Opus is slow, and the owner usually
already has an Opus session running in a terminal — a second in-line Opus just adds latency
without adding value. Latency is the product constraint.

- **Haiku** — instant: acks, wake-word confirm, narration, trivial lookups.
- **Sonnet** — the **DEFAULT** for every conversational turn. EDITH's voice.
- **Opus** — **background / explicitly-invoked deep work only** ("think about X", pasted logs,
  screen reading, brainstorm). NEVER blocks the conversation. See §Background reasoning.

When in doubt, stay on Sonnet and push depth to the background.

### Heuristics

Brain passes a `tier_hint`. Router applies the following decision: if the hint matches a tier,
it is used as-is unless the Router's own override rules fire (listed below the table).

```
┌─────────────────────────────┬────────────────────────────────────────────┐
│  tier_hint / signal          │  resolved tier → Bifrost model             │
├─────────────────────────────┼────────────────────────────────────────────┤
│  HAIKU                       │  haiku  (quick lookups, short acks, filler)│
│  SONNET                      │  sonnet (standard tasks, skills, recall)   │
│  OPUS                        │  opus   (deep reasoning, code review,      │
│                              │          planning, complex multi-step)     │
├─────────────────────────────┼────────────────────────────────────────────┤
│  Router override: escalate   │                                            │
│  HAIKU → SONNET if…          │  message token count > HAIKU_MAX_TOKENS    │
│  SONNET stays SONNET for the │  the LIVE turn is NEVER blocked on opus.   │
│  live turn; deep signal →    │  deep signal (code_review/plan/debate/long │
│  BACKGROUND opus job          │  ctx) → Brain fires async opus (see        │
│  (§Background reasoning)      │  §Background reasoning); Sonnet holds turn │
├─────────────────────────────┼────────────────────────────────────────────┤
│  Router override: demote     │                                            │
│  any → HAIKU if…             │  task_type == ack_filler                   │
└─────────────────────────────┴────────────────────────────────────────────┘
```

**Escalation to opus is gated by Guard's budget check.** Before promoting to opus, Router calls
`Guard.budget_check(tier=OPUS)`. If the check returns `deny`, Router falls back to sonnet and
annotates the response with `budget_limited=True`. See north-star §6.2.

### Default heuristic (when Brain passes tier_hint=None)

```
messages token count ≤ 500   AND task_type in {lookup, ack, filler}
  → HAIKU

any normal conversational turn (the DEFAULT)
  → SONNET   (EDITH's talking voice — latency-first)

task_type in {code_review, plan, debate, deep_analysis} OR token count > 4000
  OR a Sonnet-detected "hard" question
  → OPUS, but as a BACKGROUND job (async, non-blocking — §Background reasoning).
    The live turn still answers with SONNET ("let me think on that…").
    (subject to budget gate)
```

---

## Background reasoning (fire-and-notify opus)

The **background** counterpart to §Supervised reasoning (which is foreground + steerable). Used
when the owner kicks off deep work ("Hey EDITH, think about our sharding strategy"), pastes
logs/code, triggers screen reading, or asks a question Sonnet judges too hard for a live answer
(**auto-escalation** — the owner-chosen default, Session 3).

```
you: "think about X"   (or paste / screen-read / a detected-hard question)
      │
      ▼
  Brain.think_async(messages) ──► returns a job handle IMMEDIATELY (non-blocking)
      │
      ├─► SONNET (voice, instant): "Started a thinking session on X — I'll ping you."
      │                                        ← you keep talking / working
      │
      └─► OPUS runs in the background ──► done
                                          │
              Brain notifies: SONNET summarizes the result by voice;
              full detail persisted to Memory (remember()).
```

### Contract (interface-level)
```python
async def think_async(
    messages: list[dict],
    on_done: Callable[[ModelResponse], Awaitable[None]],  # Brain wires → Sonnet summary + Memory.remember
) -> BackgroundJob: ...   # returns immediately; exposes job.id, job.status, job.cancel()
```

### Auto-escalation (hard-question detection)
A normal turn runs on Sonnet. If a cheap complexity signal marks the question "hard" (Sonnet
self-assessment or a lightweight classifier — prototype the signal at Slice 5 build), Brain does
**not** block the turn on opus; it answers with a Sonnet holding response ("let me think on that
properly") and fires `think_async`, then reports back. Distinct from `supervised_reason`, which is
foreground and interruptible.

### Cost / autonomy
Background opus is the expensive tier → **budget-gated** (north-star §6.2) before the job starts.
Kicking off a thinking session is a read/think action → **AUTO** (no confirm gate); it writes only
to Memory, never to shared state.

---

## Two-call latency-masking pattern

This is **two separate model calls**, orchestrated to overlap in time. It is not a single
inference, not two agents sharing weights, not streaming from one model. North-star §1 states
this explicitly; this section provides the mechanics.

### Why

Opus answers take 2–5 s end-to-end. TTS audio cannot start until it has tokens to synthesize.
A haiku call completes in ~150–300 ms and can produce a spoken filler ("Sure, let me look at
that…") while opus works. The listener hears audio start immediately; the real answer follows
seconds later. Total perceived latency drops to ~200 ms even when the real answer takes 3 s.

### Timeline

```
t = 0 ms     Brain calls model_call_masked(messages, ack_prompt)
             │
             ├── Call A: haiku  ─────────────► complete ~200 ms
             │             tokens stream ────► VoiceIO.speak() starts ack audio
             │
             ├── Call B: opus   ─────────────────────────────────► stream starts ~600 ms
             │                               tokens stream ──────► VoiceIO.speak() switches
             │                                                      to real answer audio
             │
t = 200 ms   ack audio playing (haiku done)
t = 600 ms   first opus tokens → TTS begins real answer
t = 2–5 s    opus stream complete; TTS finishes
```

This is **two calls, two billing events, two latency windows.** Brain decides when to use
`model_call_masked`; the Router provides the overlapped-call mechanics.

### Division of responsibility

```
┌──────────────────┬────────────────────────────────────────────────────┐
│  Brain           │  Decides WHEN to use the masking pattern.          │
│                  │  Provides ack_prompt and full messages payload.     │
├──────────────────┼────────────────────────────────────────────────────┤
│  Router          │  Fires both calls (overlapped via asyncio.gather).  │
│                  │  Streams haiku tokens first; yields opus stream.   │
│                  │  Applies Guard.redact() to both payloads.           │
│                  │  Applies tier selection to the opus call.           │
├──────────────────┼────────────────────────────────────────────────────┤
│  VoiceIO / TTS   │  Consumes token stream; decides when to switch     │
│  (Slice 3)       │  from ack audio to answer audio.                   │
└──────────────────┴────────────────────────────────────────────────────┘
```

---

## Supervised reasoning (haiku narrator + sonnet arbiter)

An extension of the two-call pattern for long opus reasoning the owner wants to **watch and
steer** in real time (e.g. "why did the DAG fail?"). Three roles across three tiers:

- **OPUS** — the deep reasoner. Runs as a **cancellable** `asyncio.Task` and streams its
  intermediate reasoning.
- **HAIKU (narrator)** — a second, cheap consumer of opus's stream. On reasoning-step
  boundaries (**sampled, not per-token** — cost discipline) it emits one short spoken line
  ("Opus is checking the pool config now…"), delivered via `on_narration` → `VoiceIO.speak()`.
- **SONNET (arbiter)** — fires **only** when the owner interjects mid-flight. It reads the
  utterance against opus's current state and returns a `SteeringDecision`.

```
you: "why did the DAG fail?"
      │
      ▼
   Brain ──► Router.supervised_reason(messages, on_narration)
      │
  ┌───┴───────────────────┬───────────────────────────┐
  ▼                       ▼                            ▼
OPUS (cancellable)   HAIKU (narrator)           VoiceIO mic stays HOT
streams reasoning ──►summarizes live ──► speak()      │
  │                                                   │
  │   you: "no — it's the retry limit, stop" ─────────┘
  │                    │  session.steer(utterance)
  │                    ▼
  │              SONNET (arbiter)  →  CONTINUE | STOP | REDIRECT
  │                    │
  ◄──── opus_task.cancel() on STOP / REDIRECT ─────────
                       │
           REDIRECT → relaunch opus with the correction + kept partial context
```

### Steering vs. barge-in (important distinction)

A `voice.utterance` that arrives **while a `SupervisedSession` is active** is a **steering
signal**, not a fresh query. VoiceIO still cancels active TTS audio (barge-in, Slice 3 §Barge-in),
*and* the utterance is routed to `session.steer()` → the sonnet arbiter, which is the **only**
thing that can cancel the opus *task*. Barge-in alone only stops audio; it never stops the
compute. Absent an active session, an utterance is a normal new query.

### Decisions (locked defaults, tunable)

- **Narrator cadence:** sampled on reasoning-step boundaries, not per-token. Continuous
  per-token narration is a silent token-burner (see §Cost) → gated by Guard budget.
- **On STOP:** keep opus's partial reasoning as context by default (so REDIRECT builds on it);
  discard only on an explicit "start over" (`stop(keep_partial=False)`).
- **Autonomy:** STOP / REDIRECT need **no** confirm gate — the owner correcting their own query
  is inherently safe (contrast north-star §6.3, which gates shared-state writes).
- **Arbiter = sonnet:** cheap enough to run on every interjection, capable enough to parse
  intent + opus state. Haiku is too weak to arbitrate; opus too slow/costly to interrupt with.

### Cost

Worst case runs three tiers concurrently (opus + sampled haiku + occasional sonnet). Haiku
narration is the recurring cost → sampled + budget-gated; under budget pressure Guard can
downgrade narration to "silent" (log only). Sonnet fires only on an interjection.

---

## Streaming

All Bifrost calls use the streaming API so partial tokens reach callers as they arrive.

```
Router.model_call_stream()
      │  yields ModelChunk(token, is_final, usage)
      ▼
Brain or Skill consumer
      │  passes tokens to VoiceIO.speak_stream()
      ▼
TTS adapter starts synthesizing before full completion
```

- `ModelChunk.usage` carries token counts; Router passes them to Guard after the stream closes
  so Guard can update budget state.
- Callers that do not need streaming call `model_call()` which awaits the full stream internally
  and returns a `ModelResponse`.

---

## Bifrost adapter

Bifrost is Pattern's Anthropic/OpenAI-compatible gateway. The adapter is the thin layer that
translates Router's internal tier→model selection into an actual HTTP call.

### Configuration (`.env` / env vars — NOT the API key)

```
BIFROST_BASE_URL=https://bifrost.pattern.com/v1
BIFROST_PROVIDER=anthropic          # or: openai, azure — swap without code change
BIFROST_MODEL_HAIKU=claude-haiku-4-5
BIFROST_MODEL_SONNET=claude-sonnet-4-5
BIFROST_MODEL_OPUS=claude-opus-4-5
```

**The API key is NOT in `.env`.** Per north-star §6.1 (non-negotiable), EDITH's own API key is
stored in the macOS Keychain via `keyring` and loaded into memory at call time, never written
to disk or logs.

### Provider swap

`BIFROST_PROVIDER` selects the request schema (Anthropic messages format vs. OpenAI chat
completions). Swapping the backend requires only `.env` changes and a new
`BIFROST_MODEL_*` set — no Router code changes.

### Adapter interface

```python
class BifrostAdapter:
    def complete(self, model: str, messages: list[dict], stream: bool) -> ...: ...
    def stream(self, model: str, messages: list[dict]) -> AsyncIterator[str]: ...
```

Router holds one adapter instance; the adapter reads config at init and retrieves the API key
from Keychain at first call.

---

## Data model

Router is stateless. No graph nodes, no vector records, no on-disk schema.

The only persistent artifact is the **tier→model config map** (`.env` vars listed above).
Budget state lives entirely in Guard; Router reads it via `Guard.budget_check()` but does not
store it.

---

## Dependencies

- **Other slices:**
  - Slice 1 (Brain / Orchestrator) — Brain is the caller of `model_call`. Until Slice 5 ships,
    Brain uses a single-tier passthrough that satisfies the same contract.
  - Guard (Slice 1 deliverable) — `Guard.redact()` and `Guard.budget_check()` must exist before
    Router can enforce the rules.
- **Libraries:**
  - `anthropic` Python SDK (streaming, messages API) — or `openai` SDK depending on provider.
  - `keyring` — retrieve Bifrost API key from macOS Keychain.
  - `asyncio` — overlapped calls in `model_call_masked`, async streaming.
  - `python-dotenv` — load `BIFROST_*` config from `.env`.

---

## Tech choices

Defers to north-star §5 for all stack-wide choices. Additions for this slice:

| Choice | Justification |
|--------|---------------|
| `asyncio.gather` for two-call overlap | Keeps the process single-threaded; both calls are I/O-bound (HTTP). No thread pool needed. |
| Anthropic SDK streaming (`stream=True`) | Native async generator; pairs cleanly with `AsyncIterator[ModelChunk]` contract. Swap to `openai` SDK when `BIFROST_PROVIDER=openai`. |
| `.env` for model-name config | Tier→model names change with each model generation. Config, not code. |
| Keychain (not `.env`) for API key | Non-negotiable per north-star §6.1. |

---

## Autonomy & secrets notes

- **Autonomy gate:** Router has no autonomous actions. It executes calls that Brain has already
  authorized. The autonomy gate (north-star §6.3) fires in Brain/Guard *before* Router is
  invoked; Router itself is AUTO with no confirmation surface.
- **Secrets:**
  - Bifrost API key: Keychain only (`keyring.get_password`). Never in `.env`, never in logs,
    never on the bus.
  - `Guard.redact(messages)` is the first operation inside every `model_call*` method.
    Outbound payloads are redacted before the HTTP call fires. No exceptions.
  - Token streams are not logged to disk. Usage counts (integers) are passed to Guard.

---

## Cost / token notes

Per north-star §6.2, most bus events are handled locally with no model call. Router is invoked
only for events that Brain + Guard have already approved. Within Router's scope:

- **Tier discipline is the primary cost lever.** Haiku is ~25× cheaper than opus per token.
  Router defaults to the cheapest tier that fits the task.
- **Opus calls require Guard.budget_check(OPUS) to return `allow`.** If the budget is tight,
  Router falls back to sonnet and marks the response `budget_limited=True` so Brain can surface
  a warning.
- **Two-call pattern doubles the call count** for the masked interaction. The haiku ack is
  cheap; the cost of the pattern is dominated by the opus call. Acceptable only when Brain
  decides the interaction warrants it.
- Budget tracking (per-window token/cost totals, `budget.warning` events) belongs to Guard.
  Router annotates responses with token usage so Guard can update state.

---

## Build steps (high-level, ordered)

1. Implement `BifrostAdapter` — reads config from `.env`, retrieves API key from Keychain,
   exposes `complete()` and `stream()`.
2. Implement `Tier` enum and the tier→model config map.
3. Implement `model_call()` — `Guard.redact()` first, tier selection, adapter call, return
   `ModelResponse`.
4. Implement `model_call_stream()` — same redact + tier path; yield `ModelChunk` tokens.
5. Add tier override rules (escalate on token count / task_type, demote for ack_filler);
   add `Guard.budget_check()` gate before opus escalation.
6. Implement `model_call_masked()` — `asyncio.gather` on haiku ack + opus answer, return
   `(haiku_stream, opus_task)`.
7. Wire the adapter's provider-swap path (`BIFROST_PROVIDER` env var selects SDK).
8. Write unit tests covering tier routing and latency-masking overlap (see Verification).
9. Integrate with Brain: replace the single-tier passthrough used by slices 1–4.

---

## Verification / testing

Two properties to prove at build time:

### 1. Tier routing

```bash
# Parametric unit test — assert tier_hint→model mapping and override rules
pytest tests/router/test_tier_selection.py -v

# Expected: all rows pass
# HAIKU hint → BIFROST_MODEL_HAIKU
# SONNET hint → BIFROST_MODEL_SONNET
# OPUS hint → BIFROST_MODEL_OPUS (when Guard.budget_check returns allow)
# OPUS hint + budget_check=deny → BIFROST_MODEL_SONNET + budget_limited=True
# HAIKU hint + task_type=code_review → BIFROST_MODEL_OPUS (override)
# HAIKU hint + task_type=ack_filler → BIFROST_MODEL_HAIKU (no override)
```

### 2. Latency masking — ack starts before opus completes

```bash
pytest tests/router/test_latency_masking.py -v

# Test: call model_call_masked() against a mock Bifrost that delays the opus response 1 s
# Assert: first haiku token arrives before the opus response is complete
# Assert: both calls fired (two requests logged by the mock adapter)
# Assert: haiku stream and opus task returned as separate objects (not merged)
```

### Manual smoke test (after Slice 3 / VoiceIO is available)

```
1. Trigger a voice command that requires an opus answer (e.g. "review Tavishi's PR").
2. Listen: ack audio ("Sure, let me take a look at that") should start within ~300 ms.
3. Real answer audio should follow 2–5 s later without a silence gap.
4. Check logs: two Bifrost requests logged for the interaction, not one.
```

---

## Open questions

- **Redaction ownership:** Should `Guard.redact()` be called by Router as the structural
  choke-point (current spec), or should Brain redact before passing messages to Router?
  Current decision: Router calls it, so it's unbypassable regardless of caller. Open for
  owner review if Brain needs pre-redaction for other reasons (e.g. memory writes).
- **Haiku ack prompt ownership:** Is the `ack_prompt` (for `model_call_masked`) authored
  per-skill by Brain, or does Router have a default filler pool? Current assumption: Brain
  provides it. Decide in Slice 3 when VoiceIO is built and the ack experience can be tuned.
  Note: the `supervised_reason` **narrator** prompt is different — it is Router-owned and
  standard ("in one short sentence, say what the reasoning model is currently doing"),
  because it summarizes opus's live stream rather than producing generic filler.
- **Narrator step-boundary detection:** How does the narrator decide a "reasoning step"
  boundary to sample on (token count? punctuation/paragraph? opus thinking-block markers)?
  Prototype during Slice 5 build; start with a simple N-token/again-on-pause heuristic.
- **Model version pinning:** `BIFROST_MODEL_*` maps to named model strings. Should these be
  pinned to specific versions (e.g. `claude-haiku-4-5-20251001`) to avoid silent behavior
  changes on Bifrost updates? Decide at Slice 5 build time.
- **Streaming back-pressure:** If TTS is slow and the token queue grows, does Router need a
  back-pressure mechanism or is asyncio's natural flow control sufficient? Verify empirically
  during Slice 3 integration.

---

## Completion Record — Router — Session 15 (2026-07-09)

- **What shipped:** The single-tier passthrough became a real gateway: **tier selection**
  (`resolve_tier` + `TaskType`), **streaming** (`model_call_stream` → `AsyncIterator[ModelChunk]`),
  the **two-call latency-masking** mechanism (`model_call_masked` — fast ack + slower answer,
  two overlapped calls), a **budget-gate** seam before opus, and the **redaction choke-point**
  (`sanitize_text` inside every `model_call*`). Callers (slices 1–4) are unchanged — same
  `model_call` surface.

- **How it works:**
  - `edith/router/tiers.py` (now owns the `Tier` enum, moved here to break the import cycle):
    `resolve_tier(tier_hint, task_type, token_count, budget_allows_opus) -> TierDecision`.
    Rules: ACK_FILLER demotes to Haiku; HAIKU hint escalates to Sonnet over `HAIKU_MAX_TOKENS`;
    OPUS hint honored but budget-gated (deny → Sonnet + `budget_limited=True`); hint=None →
    Sonnet (the live voice), or Haiku for small cheap lookups, and a deep signal sets
    `suggest_background` (Sonnet holds the live turn; opus goes background — deferred).
  - `edith/router/bifrost.py`: `Router` gains `budget_check` + `redactor` seams (default
    allow + `sanitize_text`). `model_call` unchanged in behaviour but now redacts + resolves the
    tier first. `model_call_stream` parses the Anthropic SSE (`content_block_delta` text +
    `message_start`/`message_delta` usage) and yields `ModelChunk`s. `model_call_masked` fires
    the answer as a `Task` and pumps the ack stream through a queue via a started task, so **both
    HTTP requests are issued before either stream is drained** (true overlap, not ack-then-answer).

- **Key decisions made during build:**
  - **`model_call_masked` is tier-parameterized, answer defaults to Sonnet — NOT opus.** The spec
    §Timeline drew "Call B: opus", but the revised §Tier-selection philosophy (Sonnet is the live
    voice, opus never blocks) contradicts that. Per the file's own "fix the section" reminder, the
    masker is a tier-agnostic *mechanism*; opus-in-background is `think_async` (deferred).
  - Left the working non-streaming `model_call` POST intact; added streaming alongside rather than
    routing everything through SSE (callers depend on the POST path; zero functional gain, real risk).
  - Redaction moved INTO Router as the unbypassable choke-point (Brain still redacts too — safe).

- **Deviations from spec + why:**
  - Build steps 7 (OpenAI provider-swap) deferred — Bifrost is Anthropic-compatible today; the
    `BIFROST_PROVIDER` seam is config-only, no second SDK path built.
  - `Tier` enum relocated from `bifrost.py` to `tiers.py` (import-cycle break); re-exported from
    `edith.router`, so all callers are unaffected.

- **Files created / changed:** NEW `edith/router/tiers.py`, `tests/test_router_tiers.py`,
  `tests/test_router_stream.py`. CHANGED `edith/router/bifrost.py` (ModelChunk, streaming, masking,
  seams; `Tier` now imported from tiers), `edith/router/__init__.py` (exports).

- **Verification / tests run + results:** **212 passed, 1 skipped** (+ the router live smokes);
  ruff + pyright clean. New: 10 tier-selection tests, 7 streaming/masking/redaction tests. Masking
  test proves TRUE overlap (2 requests issued before draining). **LIVE-smoked: `model_call_stream`
  against REAL Bifrost (`--run-live`) yielded real tokens with a correct final chunk** — the SSE
  parser is verified against the actual event stream, not just the mock.

- **Follow-ups / known gaps:**
  - **`supervised_reason` / `SupervisedSession` (3-tier steerable reasoning) — NOT built.** Deeply
    coupled to live-voice barge-in (owner-smoke) and speculative; not in the spec's ordered build
    steps. Deferred with the contract as the seam.
  - **`think_async` / background-opus auto-escalation — NOT built (UNMET).** This is the centerpiece
    of the routing philosophy ("opus never blocks, goes background"); `resolve_tier` already returns
    `suggest_background`, but nothing acts on it yet. The finder/resolve has a fire-and-forget
    background-opus pattern to reuse. Named here as a real gap, like Guard/audio.
  - **Masking + streaming have no live consumer yet.** The perceived-latency payoff needs
    `VoiceIO.speak_stream` + an `edithd` composition root; the mechanics are unit- and live-tested,
    but the end-to-end "ack audio then answer audio" is owner-smoke once those exist.
  - **Guard** still deferred: `budget_check` defaults to allow, redaction uses `sanitize_text`.
  - `BIFROST_MODEL_*` version pinning + OpenAI provider path: config seams, not built.
