# 12 — Router background reasoning

> Companion to `05-router.md`. Implements the deferred/UNMET background-reasoning seam that
> `resolve_tier`'s `suggest_background` flag has been pointing at with nothing acting on it.
> Honest-framing (per north-star §1): "two agents, fast masks slow" = **two separate model
> calls orchestrated in time**, never one inference and never shared weights.

Module: `edith/router/background.py`, exported from `edith.router`. Only these two router files
were touched; Brain / edithd / voice consumers are lead-wired.

---

## `supervised_reason` — draft-then-review (SYNCHRONOUS, built + tested)

```python
async def supervised_reason(
    router, messages, *,
    draft_tier=Tier.SONNET, review_tier=Tier.OPUS, max_tokens=_DEFAULT_MAX_TOKENS,
) -> ModelResponse
```

Two sequential `router.model_call`s:

1. **Draft** — fast tier (Sonnet) answers `messages`.
2. **Review** — strong tier (Opus) gets the *full original context* with the draft folded in
   as an assistant turn, then a Router-owned user instruction to critique + improve it. The
   REFINED (second) response is returned.

```
messages ──► model_call(draft_tier)  ──► draft.text
                                            │
   [ ...messages,                           │  folded in as assistant turn
     {assistant: draft.text},   ◄───────────┘
     {user: "critique + improve the draft above, return the improved answer"} ]
                │
                ▼
        model_call(review_tier)  ──► REFINED ModelResponse   (returned)
```

**Why the draft is folded in as an assistant turn:** the review pass must *see* the draft so it
refines rather than re-answers blind. The load-bearing test asserts the draft text appears in
the review call's payload.

**Consumer:** Brain's deep-query path (lead wires the call site). This one is production-shaped.

---

## `think_async` — background opus (seam built + tested; UNFINISHED as a feature)

```python
async def think_async(
    router, messages, *,
    on_result: Callable[[ModelResponse], Awaitable[None]] | None = None,
    tier=Tier.OPUS, max_tokens=_DEFAULT_MAX_TOKENS,
) -> asyncio.Task[ModelResponse]
```

Schedules a background opus `model_call` as an `asyncio.Task`, returned immediately so the live
turn is never blocked (Sonnet holds the turn — spec 05 latency-first). On completion, if
`on_result` is set, it is awaited with the response. The task is *returned* (caller owns its
lifetime), so there is no fire-and-forget orphan and no `RUF006` noqa.

### ⚠ Consumer gap — the honest status

**`think_async` has NO production consumer yet. Status: seam built + tested; speak-later
consumer is lead-wired, owner-smoke, UNFINISHED.**

The default `on_result=None` is deliberately **not** a consumer — wiring a no-op there would just
recreate the `suggest_background` dead-seam (`05-router.md`) one level down: a flag/hook that
nothing acts on. The task runs and the result stays retrievable via the returned task, but
nothing *speaks* it.

### The gate-interaction problem (why the consumer is non-trivial)

A background answer arrives ~20 s after the owner moved on. Speaking it is not "call TTS" — it
has to pass the voice half-duplex interaction the lead owns:

```
opus done (~20s later) ──► on_result(response)
                                │  must NOT just speak immediately
                                ▼
              ┌───────────────────────────────────────────────┐
              │ half-duplex gate  — is EDITH/owner mid-utterance?│
              │ cooldown          — enough silence to interject? │
              │ conversation-window — still the same topic/turn? │
              │ echo-suppression  — don't trigger our own wake?  │
              └───────────────────────────────────────────────┘
                                │  all clear
                                ▼
                     Sonnet summarizes by voice + Memory.remember()
```

Until that interaction is designed and wired (lead, then owner-smoke), `think_async` is a tested
seam, not a shippable feature. Do not describe it as production-complete.

---

## Tests — `tests/test_router_background.py` (headless, fake router)

Deterministic: a `_FakeRouter` duck-types `model_call`, records each `(messages, tier, max_tokens)`,
returns canned responses. No httpx, no sleeps, no network.

- `supervised_reason`: two calls at draft/review tiers; the **draft text appears in the review
  payload**; the returned `.text` is the refined (second) response; custom tiers + `max_tokens`
  honored.
- `think_async`: returned task is an `asyncio.Task` yielding the response on opus; `on_result`
  awaited with the same response when set; with `on_result=None` the task still runs and the
  result is retrievable (the honesty case).
