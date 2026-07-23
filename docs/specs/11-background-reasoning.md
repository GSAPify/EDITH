# 11 — Background reasoning (`think_async` — fire-and-notify opus)

> Closes the routing philosophy's UNMET centerpiece (spec 05 §Background reasoning): **opus never
> blocks the live turn — it goes to the background and pings when done.** This slice ships
> `think_async`: a budget-gated, cancellable, tracked background opus job that answers deep work
> off the critical path and reports back by voice + `remember()`.

## Purpose

EDITH's talking voice is Sonnet — latency-first (spec 05 §Routing philosophy). When the owner kicks
off deep work ("Hey EDITH, think about our sharding strategy") or pastes a big log to reason over,
EDITH must **not** block the conversation on a 2–5 s opus call. It answers/acks immediately on
Sonnet, fires opus in the background, keeps the mic free, and later speaks a short summary of what
opus concluded — persisting the full detail to Memory.

Two halves that didn't meet before:
- `resolve_tier` computes a `suggest_background` flag for deep/long turns, but nothing consumed it
  (and it was unreachable on the live path — Brain always hinted Sonnet, which short-circuits it).
- `edith/finder/resolve.py` has a proven fire-and-forget background-opus pattern (`_deep_extract`
  via `asyncio.create_task`), but it is single-purpose and **untracked** — no job handle, no status,
  no cancellation, no shutdown ownership.

This slice generalizes that pattern into a first-class, tracked mechanism.

## Design decisions (the ones that gated this)

1. **Placement mirrors `model_call_masked`.** The *mechanism* (budget-gate opus → tracked opus task
   → `on_done` → `BackgroundJob`) lives in the router layer as a thin `BackgroundReasoner`
   (`edith/router/background.py`) that owns the job registry. **Brain owns the triggers and supplies
   `on_done`.** This is the exact division of responsibility the masking method already uses (Brain
   decides WHEN, Router provides the mechanism); budget-gating is already a Router concern; injecting
   `on_done` keeps the mechanism ignorant of Memory. Spec 05's diagram `Brain.think_async(...)` is the
   *conceptual* entry point — implemented as Brain calling the reasoner, not a second copy.

2. **Tracked tasks, not fire-and-forget.** Unlike `resolve.py`'s `asyncio.create_task(...)  # noqa
   RUF006`, a `BackgroundJob` exposes `.status` and `.cancel()`, so its task is **held in a registry**
   (a `set`, with a done-callback that discards). A detached task with no live reference can be
   garbage-collected mid-flight — copying the fire-and-forget pattern here would be a bug.

3. **Budget-gate BEFORE starting.** Background opus is the expensive tier (north-star §6.2). If
   `budget_check(OPUS)` denies, the job **never starts** — `think_async` returns
   `BackgroundJob(status=DENIED)`. It does NOT silently downgrade to a background Sonnet job (Sonnet
   already answered the live turn; a background Sonnet re-run would be pointless spend). Guard's real
   budget is still deferred → the check defaults to allow.

4. **Background errors are caught, not swallowed silently.** The job body wraps the opus call in
   `try/except MODEL_CALL_ERRORS` (the Router's declared transport-failure tuple — a specific catch,
   never bare) and sets `status=FAILED`; `on_done` fires **only** on success. `asyncio.CancelledError`
   is *not* in that tuple, so cancellation propagates and sets `status=CANCELLED`.

5. **Two triggers in Brain — explicit phrase + deep input.**
   - **Explicit:** the imperative `think about X` / `think on X` (regex mirroring `_REPO_PHRASE`).
     A negative lookbehind `(?<!you )` excludes the conversational "(what do) **you** think about X",
     an opinion question that wants a live answer. Fires the background job, speaks a holding ack,
     and skips the normal live answer.
   - **Passive (deep input):** after the normal live Sonnet answer, if the **owner's utterance
     itself** is large (`> DEEP_TOKENS`, measured on the utterance ALONE), also fire a background
     opus pass. **Measured on the utterance, not the assembled context** — see §Cost-safety.

6. **Dedicated completion event.** `on_done` publishes `brain.background_done` (not `brain.decision`),
   so the daemon can distinguish a background result from a live answer. Voiced path only.

7. **`on_done` persists BEFORE it summarizes.** The expensive opus detail is `remember()`ed first and
   unconditionally; the Sonnet summary call is wrapped so a transport blip on the summary can't
   discard the deep work — it just skips the spoken ping that once. Order:
   `remember → (try) summarize → publish ping`.

8. **Shutdown ownership.** A background opus job can outlive the turn that started it. The daemon
   calls `reasoner.cancel_all()` in `stop()`, alongside the existing `_session_tasks` / `_voice_task`
   cancellations, so Ctrl-C / `kill` never leaves an opus call dangling.

## Cost-safety (why the passive trigger measures the utterance, not the context)

The obvious wiring — consume `resolve_tier`'s `suggest_background` — measures the **whole assembled
payload**: `VOICE_PERSONA` + all recalled facts + the entire `TurnBuffer` history + the utterance.
In a real voiced session that crosses ~16k chars within a few turns, so *every* subsequent turn —
including "what time is it?" — would trip `> 4000 tokens` and spawn a background opus job. With
Guard deferred, `budget_check` defaults to allow, so nothing would throttle it: an unbounded opus
cost leak. So the passive trigger keys off the **utterance's own** token count. "Deep" then means
what it should — a pasted log / stack trace / long question — not "the conversation got long." This
is a deliberate deviation from the `suggest_background` flag; the flag stays in `resolve_tier` as the
seam for the deferred Sonnet-self-assessment auto-escalation, which will judge difficulty properly.

## Interface

```python
# edith/router/background.py  (new)

class JobStatus(Enum):
    RUNNING | DONE | FAILED | DENIED | CANCELLED

@dataclass
class BackgroundJob:
    id: str
    status: JobStatus
    task: asyncio.Task[None] | None = None   # None for a DENIED job (never started)
    def cancel(self) -> None: ...            # cancels the task (no-op if absent/finished)

class BackgroundReasoner:
    def __init__(self, router, *, budget_check=lambda _t: True) -> None: ...
    async def think_async(self, messages, on_done, *, max_tokens=1024) -> BackgroundJob: ...
    def cancel_all(self) -> None: ...        # daemon shutdown; cancels every outstanding job
```

Brain gains an injected `reasoner: BackgroundReasonerLike | None = None` (default None → never
backgrounds, standalone behavior preserved) and the two triggers + `on_done`. **No change to
`bifrost.py` / `ModelResponse` / `model_call`** — the passive trigger reads the utterance directly,
so the flag-plumbing that an earlier draft added was removed.

## Changes

- **`edith/router/background.py` (new):** `JobStatus`, `BackgroundJob`, `BackgroundReasoner`.
- **`edith/router/__init__.py`:** export `BackgroundReasoner`, `BackgroundJob`, `JobStatus`.
- **`edith/brain/loop.py`:** injected `reasoner`; `_THINK_PHRASE`; explicit + passive triggers;
  `on_done` closure (remember → summarize → ping); `_is_deep_input` (utterance-token gate);
  `_build_messages` / `_answer` helpers factored out of `_on_utterance`.
- **`edith/daemon/edithd.py`:** build `self._reasoner = BackgroundReasoner(self._router)`, inject into
  Brain; subscribe `brain.background_done` → `_speak_background` (voiced only); `cancel_all()` in
  `stop()`.

## Verification / testing

TDD. `edith/` source stays pyright-clean; the full suite + ruff pass.

- **Reasoner (`test_router_background.py`):** a slow fake opus proves `think_async` returns a RUNNING
  job *before* opus completes; `on_done` fires once with the result (DONE); `cancel()` → CANCELLED,
  `on_done` never fires; budget-deny → DENIED with **zero** model calls; a `MODEL_CALL_ERRORS` opus →
  FAILED, no `on_done`; `cancel_all` cancels every job; job ids unique.
- **Brain (`test_brain_background.py`):** explicit "think about X" acks + backgrounds + skips the live
  answer; a deep-input turn answers live AND backgrounds; a trivial turn with huge *recall* does NOT
  background (cost-safety); `on_done` remembers + summarizes + pings; a summary failure still leaves
  the detail persisted (no ping); "what do you think about X" is a live answer; no-reasoner is a
  plain turn.
- **Daemon (`test_daemon_background.py`):** builds + injects the reasoner; `brain.background_done` →
  `VoiceIO.speak` (voiced only); `stop()` cancels an in-flight job.

## Deferred / risks

- **`supervised_reason` / `SupervisedSession` — NOT built.** Barge-in-coupled, owner-smoke-only,
  speculative; contract stays the seam (spec 05 §Follow-ups).
- **Sonnet self-assessment auto-escalation — NOT built.** The utterance-size heuristic is the only
  passive trigger this slice ships; a per-turn "is this hard?" model call is extra spend and
  speculative. `suggest_background` remains in `resolve_tier` as its seam.
- **The `(?<!you )` guard also suppresses "can you think about X" / "could you think about X".** A
  deliberate trade: it kills the common "(what do) you think about X" opinion false-positive at the
  cost of routing those (rarer) imperative forms to a normal live answer. A false negative just means
  a live answer instead of a background think — acceptable for a thin heuristic.
- **A job that completes during a *later* pause still speaks + writes Memory.** `_on_utterance` is
  pause-gated at entry, but `on_done` fires whenever opus lands; `cancel_all` covers shutdown, not a
  plain pause. Low severity (the think was authorized when started; delivering its result after a
  transient pause is arguably correct) — noted, not gated.
- **Live consumer is owner-smoke.** End-to-end "ack now, opus pings later by voice" needs a mic + a
  real Bifrost opus call.
- **Guard still deferred** — `budget_check` defaults to allow.

## Completion Record — 11 background-reasoning — 2026-07-23

- **Built:** `BackgroundReasoner` + `BackgroundJob` + `JobStatus` (`edith/router/background.py`,
  exported); Brain explicit + passive(deep-input) triggers, `on_done` (remember→summarize→ping),
  `_is_deep_input`; daemon composition (`BackgroundReasoner(router)` injected, `brain.background_done`
  → `_speak_background` voiced-only, `cancel_all()` in `stop()`). **No `bifrost.py` change.**
- **Tests:** 322 passed, 2 skipped (live), ruff clean, pyright at baseline (16 pre-existing test-fake
  errors, `edith/` source + all three new test files 0). 20 new tests across the three files.
- **Deviation from the brainstorm:** the owner chose "passive = the `suggest_background` flag," but the
  flag measures total assembled context, which would auto-fire opus on every turn once a session grew
  (unbounded cost with Guard deferred). Changed the passive trigger to measure the utterance itself
  (§Cost-safety) — same goal (auto-background a deep turn), cost-safe. The flag plumbing that an
  earlier commit added to `ModelResponse`/`model_call` was reverted; the diff is Brain + daemon + the
  new reasoner only.
- **Owner LIVE-SMOKE still pending:** real mic → "think about X" → opus pings back by voice; and a
  real pasted-log deep-input turn.
