# 11 — Background reasoning (`think_async` — fire-and-notify opus)

> Closes the routing philosophy's UNMET centerpiece (spec 05 §Background reasoning): **opus never
> blocks the live turn — it goes to the background and pings when done.** `resolve_tier` has always
> returned `suggest_background`, but nothing acted on it (and, on the live path, Brain never even
> received it — see §The premise correction). This slice ships `think_async`: a budget-gated,
> cancellable background opus job that answers deep work off the critical path and reports back by
> voice + `remember()`.

## Purpose

EDITH's talking voice is Sonnet — latency-first (spec 05 §Routing philosophy). When the owner kicks
off deep work ("Hey EDITH, think about our sharding strategy") or asks a question heavy enough that
Sonnet's live answer isn't the last word, EDITH must **not** block the conversation on a 2–5 s opus
call. It answers/acks immediately on Sonnet, fires opus in the background, keeps the mic free, and
later speaks a short summary of what opus concluded — persisting the full detail to Memory.

Two halves that don't meet today:
- `resolve_tier` computes `suggest_background=True` for deep/long turns, but **nothing consumes it**,
  and Brain never sees it anyway (§The premise correction).
- `edith/finder/resolve.py` has a proven fire-and-forget background-opus pattern (`_deep_extract`
  via `asyncio.create_task`), but it is single-purpose (repo deep-extract) and untracked — no job
  handle, no status, no cancellation, no shutdown ownership.

This slice generalizes that pattern into a first-class, tracked mechanism.

## The premise correction

`suggest_background` is **not** reachable on the live path today. In `tiers.py`, the
`tier_hint is Tier.SONNET` branch early-returns `TierDecision(Tier.SONNET)` **before** the deep-task
logic, and `Brain._on_utterance` always calls `model_call(..., _DEFAULT_TIER)` with
`_DEFAULT_TIER = Tier.SONNET`. So on every live turn the flag is `False`. It is only ever set on the
`tier_hint=None` path, which Brain never took. Consequences for this design:

1. The **primary trigger is explicit detection in Brain** (a `think about X` / `think on X` regex,
   mirroring `_REPO_PHRASE`), not passive consumption of an already-flowing flag.
2. The **passive trigger** requires Brain to switch its answer call to `tier_hint=None` and read the
   resolved decision back off `ModelResponse`. That changes existing Brain answer-path tests
   (accepted — the live tier stays Sonnet for normal turns because `task_type` defaults to `GENERAL`;
   the only new auto-fire is a `> DEEP_TOKENS` (4000-token) turn).

## Design decisions (the ones that gated this)

1. **Placement mirrors `model_call_masked`.** The *mechanism* (budget-gate opus → tracked opus task
   → `on_done` → `BackgroundJob`) lives in the router layer as a thin `BackgroundReasoner`
   (`edith/router/background.py`) that owns the job registry. **Brain owns the trigger and supplies
   `on_done`.** Discriminating reasons: it is the exact division of responsibility the masking method
   already uses (Brain decides WHEN, Router provides the mechanism); budget-gating is already a Router
   concern; injecting `on_done` keeps the mechanism ignorant of Memory. The spec 05 diagram's
   `Brain.think_async(...)` is the *conceptual* entry point — implemented as Brain calling the
   reasoner, not a second copy of the machinery.

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
   is *not* in that tuple, so cancellation propagates and sets `status=CANCELLED`. An unhandled
   exception in a detached task would vanish and `on_done` would never fire — this prevents that.

5. **Dedicated completion event.** `on_done` publishes `brain.background_done` (not `brain.decision`),
   so the daemon (and any future consumer) can distinguish a background result from a live answer.
   The daemon subscribes it to `VoiceIO.speak` on the voiced path only.

6. **Shutdown ownership.** A background opus job can outlive the turn that started it. The daemon
   calls `reasoner.cancel_all()` in `stop()`, alongside the existing `_session_tasks` / `_voice_task`
   cancellations, so Ctrl-C / `kill` never leaves an opus call dangling.

## Interface

```python
# edith/router/background.py  (new)

class JobStatus(Enum):
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    DENIED = "denied"       # budget denied opus → never started
    CANCELLED = "cancelled"

@dataclass
class BackgroundJob:
    id: str
    status: JobStatus
    def cancel(self) -> None: ...   # cancels the underlying asyncio.Task (no-op if finished)

class BackgroundReasoner:
    def __init__(self, router: RouterLike, *, budget_check: BudgetCheck = lambda _t: True) -> None: ...

    async def think_async(
        self,
        messages: list[dict[str, object]],
        on_done: Callable[[ModelResponse], Awaitable[None]],
        *,
        max_tokens: int = ...,
    ) -> BackgroundJob:
        """Budget-gate OPUS, fire a tracked opus task, return a handle immediately.
        On success: status=DONE, await on_done(result). On MODEL_CALL_ERRORS: status=FAILED.
        On budget deny: return status=DENIED without starting."""

    def cancel_all(self) -> None:   # daemon shutdown; cancels every outstanding job
```

```python
# edith/router/bifrost.py  (edit)
class ModelResponse:
    ...
    suggest_background: bool   # NEW — set from the resolved TierDecision
    tier: Tier                 # NEW — the tier actually used, for accurate brain.decision reporting

async def model_call(self, messages, tier_hint: Tier | None, ...) -> ModelResponse: ...
#                                              ^^^^^^^^^^^^^ widened so Brain can pass None
```

## Changes

- **`edith/router/background.py` (new):** `JobStatus`, `BackgroundJob`, `BackgroundReasoner`.
- **`edith/router/bifrost.py`:** `ModelResponse` gains `suggest_background` + `tier`; `model_call`
  sets both from the `TierDecision` and widens `tier_hint` to `Tier | None`.
- **`edith/router/__init__.py`:** export `BackgroundReasoner`, `BackgroundJob`, `JobStatus`.
- **`edith/brain/loop.py`:**
  - `RouterLike.model_call` `tier_hint` widened to `Tier | None`.
  - New `BackgroundReasonerLike` protocol + injected `reasoner: BackgroundReasonerLike | None = None`
    (default None → standalone Brain never backgrounds; both trigger paths become no-ops).
  - `_THINK_PHRASE` regex; **explicit path** (ack now + fire background, skip normal answer) and
    **passive path** (normal Sonnet answer via `tier_hint=None`, then fire background when
    `response.suggest_background`).
  - `on_done` closure: Sonnet-summarize the opus result → `sanitize_text` → `remember()` full detail
    → publish `brain.background_done`.
- **`edith/daemon/edithd.py`:** build `self._reasoner = BackgroundReasoner(self._router)` in
  `start()`, inject into Brain; subscribe `brain.background_done` → new `_speak_background` (voiced
  path only); `stop()` calls `self._reasoner.cancel_all()`.

## Verification / testing

TDD. The crux test mirrors the masking method's "true overlap" proof:

- **Non-blocking:** a slow fake opus (an `asyncio.Event` the test controls) proves `think_async`
  returns a `RUNNING` job *before* the opus call completes.
- **Notify:** once released, `on_done` fires exactly once with the opus `ModelResponse`; status → DONE.
- **Cancel:** `job.cancel()` cancels the task, status → CANCELLED, `on_done` never fires.
- **Budget deny:** `budget_check` returning False yields `status=DENIED` and issues **zero** model
  calls (assert the fake router was never called).
- **Failure:** a fake opus raising a `MODEL_CALL_ERRORS` member → status=FAILED, `on_done` not fired,
  no unhandled-exception warning.
- **Brain explicit path:** "think about X" → holding-ack `brain.decision` published, `think_async`
  invoked, normal answer path skipped.
- **Brain passive path:** a `> DEEP_TOKENS` turn → normal Sonnet `brain.decision` published AND
  `think_async` invoked; a normal short turn fires neither background call.
- **Brain no-reasoner:** `reasoner=None` → neither path backgrounds (standalone behavior preserved).
- **Daemon:** `brain.background_done` → `VoiceIO.speak`; `stop()` cancels outstanding jobs.

Full suite + ruff + pyright must stay clean.

## Deferred / risks

- **`supervised_reason` / `SupervisedSession` (3-tier steerable reasoning) — NOT built.** Barge-in-
  coupled, owner-smoke-only, speculative; contract stays the seam (spec 05 §Follow-ups).
- **Sonnet self-assessment auto-escalation — NOT built.** The only auto trigger this slice ships is
  the `> DEEP_TOKENS` heuristic. A per-turn "is this hard?" model call is extra spend and speculative;
  deferred.
- **Failure notification is silent.** A FAILED job sets status but speaks nothing (no `on_done`). A
  spoken "that thinking session failed, sir" ping is a documented follow-up, not this PR.
- **Live consumer is owner-smoke.** The end-to-end "ack now, opus pings later by voice" needs a mic
  and a real Bifrost opus call — unit-tested here, owner LIVE-SMOKE once running.
- **Guard still deferred** — `budget_check` defaults to allow.

## Completion Record — 11 background-reasoning — (pending)

_(filled in at end of build: files touched, test counts, ruff/pyright status, any deviations.)_
