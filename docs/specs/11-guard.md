# 11 — Guard

> **Honest-framing reminder:** no unicorns. Guard is a pure policy + counter object —
> no model magic, no I/O. It decides and counts; other subsystems act on its verdicts.
>
> Architecture-level interfaces + cross-cutting rules are fixed in `00-north-star.md`
> (§6.1 secrets, §6.2 budget, §6.3 autonomy gate) — **do not restate them, reference
> them.** This file adds implementation depth for the Guard unit only.

## Purpose

Guard is the north-star §6 cross-cutting enforcement point. Its three duties are
**redact**, **authorize**, and **budget**. This slice ships `authorize` and the token
budget as one headless policy object (`edith/guard/guard.py`). Redaction (§6.1) is
already owned by `edith.memory.secrets.sanitize_text` and enforced at the Router's
outbound choke-point (`bifrost.py::_redact_messages`); Guard does **not** duplicate it.

Guard is **pure**: no I/O, no model calls, no bus. This keeps its policy trivially
testable and lets it be constructed once and injected wherever a decision or a counter
is needed.

## API

```python
class Decision(Enum):
    ALLOW = "allow"
    ASK   = "ask"
    DENY  = "deny"

class Guard:
    def __init__(
        self,
        denylist: set[str] | frozenset[str] | None = None,
        *,
        token_budget: int = 1_000_000,       # tokens per window (daily default)
        window_seconds: float = 86_400.0,    # 1 day
        clock: Callable[[], float] = time.monotonic,
    ) -> None: ...

    def authorize(self, action: str, *, needs_confirmation: bool = False) -> Decision: ...
    def record(self, tokens_in: int, tokens_out: int) -> None: ...
    def budget_check(self, tier: Tier) -> bool: ...   # True == within budget
    def budget_used(self) -> int: ...                 # tokens used this window
```

`Tier` is imported from `edith.router.tiers` (the same internal import `bifrost.py`
uses; no cycle since `tiers` imports nothing back).

## Autonomy gate policy (§6.3)

`authorize` is pure and has a strict precedence — **DENY wins over ASK**:

1. `action in denylist` → **DENY** (even if `needs_confirmation=True`).
2. else `needs_confirmation` → **ASK**.
3. else → **ALLOW**.

Membership is **exact** on a *normalized action verb* (e.g. `"force_push"`,
`"drop_table"`) — not substring/verb-in-command matching, so denying `"rm"` does not
deny `"confirm"`. Caller contract: pass the action's canonical verb, not a raw shell
string. The default denylist (`force_push`, `drop_table`, `rm_rf`, `shutdown`,
`disk_wipe`) mirrors the §6.3 "deny/ask-first" column and the owner's CLAUDE.md
guardrails; the lead or a skill can override it via the constructor.

## Budget model (§6.2)

A single rolling window of `window_seconds` (default one day) with a `token_budget`
cap. `record(tokens_in, tokens_out)` accumulates `budget_used()`. The window uses a
**sliding reset**: when the injected `clock()` shows the window has elapsed, usage
resets to 0 and the window restarts at *now* (not aligned to a fixed boundary). The
rollover check runs at the top of `record`, `budget_check`, **and** `budget_used`, so a
pure read after the clock advances observes the reset without needing a `record`. The
clock is injectable (defaults to `time.monotonic`, immune to wall-clock jumps) so tests
drive rollover deterministically.

**Tier-aware `budget_check` (how OPUS is treated):** OPUS costs more, so it is cut off
**before** Sonnet/Haiku. HAIKU/SONNET are within budget while `budget_used() <
token_budget`; OPUS is within budget only while `budget_used() < token_budget * 0.75`.
This reserves the budget tail for the live voice — Sonnet holds every turn — and starves
only the expensive background/deep work first. It composes exactly with the Router: an
explicit `OPUS` hint that fails `budget_check(Tier.OPUS)` falls back to Sonnet with
`budget_limited=True` (see `05-router.md` / `tiers.py::resolve_tier`).

The default `token_budget` (1,000,000/day) is a generous-but-real ceiling: enough
headroom that normal use never trips it, low enough that a runaway narration loop is
caught within a day. The exact number is a config knob; the governance is the point.

## Wiring (lead)

Guard is built as an isolated unit. Two integration touchpoints were **deliberately not
done here** — the lead wires them after this lands:

1. **Router (`edith/router/bifrost.py`)** — construct `Router` with
   `budget_check=guard.budget_check` (it already declares the
   `BudgetCheck = Callable[[Tier], bool]` seam, defaulting to allow-all), and call
   `guard.record(resp.input_tokens, resp.output_tokens)` after each `model_call` so the
   window counter reflects real spend.

2. **Daemon (`edith/daemon/edithd.py`)** — replace the `_ZeroBudget` stub (whose
   `budget_used()` returns 0) with the Guard instance so the Control API `status.budget_used`
   reports real usage.

Redaction needs no wiring change — it already runs in the Router choke-point.

## Tests

`tests/test_guard.py`: authorize allow/ask/deny paths, DENY-wins-over-ASK collision,
exact-membership (no substring), non-empty default denylist; budget accumulation,
`budget_used`, `budget_check` flipping false past the cap, the discriminating
**OPUS-cut-off-before-Sonnet-at-the-same-usage** case, and window rollover (reset on a
pure read, no-roll-before-elapsed, fresh window after roll) via an injected clock.

## Completion Record — 11 guard (wiring) — 2026-07-27

Guard shipped in PR #17 and was constructed by **nobody** — every gate defaulted to
permissive and nothing ever called `record()`. This lands the wiring.

- **One Guard per daemon.** `edith/daemon/__main__.py` builds it **before** `_build_router`
  so the sharing is structural, then hands the same instance to the `Router` and to
  `EdithDaemon(guard=…)`. A per-subsystem Guard would give each its own window, which is not
  a budget. `EdithDaemon`'s `budget: BudgetView` parameter and the `_ZeroBudget` stub are
  **deleted** (zero callers) — one parameter, one meaning.
- **The charge path (the crux).** `Router` gained an `on_usage: Callable[[int, int], None]`
  seam mirroring the existing `budget_check` / `redactor` style, wired to `Guard.record`.
  It fires in `model_call` (after parse) and in `model_call_stream` (just before the final
  chunk). `model_call_masked` needs **no third call site** — it delegates to those two, so
  both of its billing events are charged automatically; a comment says so, to stop a future
  "fix" from double-billing.
- **Streaming usage is real, not a hole.** Anthropic SSE reports usage across `message_start`
  (`input_tokens`) and `message_delta` (`output_tokens`), which `_usage_of` already
  accumulated — the repo's own stream fixture emits both. A field the gateway omits reads as
  0 rather than being guessed at (test: `..._when_the_gateway_omits_usage`). The one uncharged
  path is a caller that abandons the generator mid-stream; `model_call_masked`'s pump always
  drains, so nothing in-tree hits it.
- **Four seams wired.** `Router.budget_check` and `BackgroundReasoner(budget_check=…)` take
  `Guard.budget_check` directly (signatures already matched). `Narrator.budget_gate` is
  zero-arg, so it is bound to `Tier.HAIKU` — the tier `_narrate_error` actually calls at.
  `Guard` satisfies `control.BudgetView` structurally, so `status.budget_used` is now real
  spend (this is the number PR #25's menu-bar label renders).
- **Desktop autonomy gate — a NEW call site.** `DesktopControlSkill` took a `guard` seam and
  puts the parsed action's verb (`Intent.value`: `open_app` / `spotify` / `terminal` /
  `omc_launch`) to `authorize` **before any `Runner` call**, so a refused action has no OS
  side effect. `handled=True` on refusal — refusing IS handling the turn; falling through
  would have Brain answer an utterance she just declined to act on. None of the four verbs
  are in the default denylist, so stock behaviour is unchanged.
- **ASK is mapped to DENY, fail-closed.** There is no voice-confirm implementation anywhere
  in the repo: `PRReviewSkill`'s injected `Confirm` callable defaults to `_deny` and the
  daemon wires that default. So ASK cannot be honoured and must not be assumed — EDITH
  refuses and *says why* rather than half-acting or going quiet. **A real voice-confirm
  ("should I?" → listen for yes) is its own item**; it is the sole reason ASK collapses here.
  When the gate is reached the haiku classify fallback has already fired for an
  otherwise-unparsed utterance — one cheap model call for an action then refused. Accepted:
  the gate needs a parsed verb, and the denylist win is worth it.

### Budget-exhausted behaviour, per seam

| Seam | At exhaustion | User-visible |
|---|---|---|
| `Router.budget_check` | only ever consulted with `Tier.OPUS` (in `_resolve`), so an opus hint falls back to Sonnet with `budget_limited=True` | live answer unaffected |
| `BackgroundReasoner` | job is `DENIED` and never starts | Brain speaks `_THINKING_DENIED` instead of the ack |
| `Narrator.budget_gate` (HAIKU) | model-gated branch skipped → SPOKEN-LOCAL template | she still speaks, just no model call |
| `BudgetView` | n/a — a read | menu bar shows real usage instead of 0 |

**Load-bearing property:** because the Router only ever gates `Tier.OPUS`, a fully exhausted
budget can never block a live Sonnet/Haiku call at the Router layer. **EDITH cannot go mute
from budget exhaustion.** Everything degrades around the live voice, which is exactly what
Guard's `_OPUS_RESERVE_FRACTION` was designed to buy.

**One behaviour fix this forced (`brain/loop.py`).** The explicit "think about X" path fired
the background job and unconditionally spoke "I'll ping you when I have something", then
returned. With a real budget a `DENIED` job never starts, so the owner would have been
promised a ping that could never arrive — a silent drop, the exact failure mode to avoid.
`_start_background` now returns the `BackgroundJob` and the caller speaks `_THINKING_DENIED`
when it came back `DENIED`. The turn's shape is otherwise unchanged (it does not fall through
to a live answer). The passive deep-input path needs no change: the live turn already answered.

- **Tests:** **361 passed, 2 skipped** (348 baseline + 13 new in `tests/test_guard_wiring.py`),
  `ruff check edith tests` clean. The new file proves the wiring, not the policy
  (`test_guard.py` still owns policy): the charge path decrements a real Guard through all
  three `model_call*` variants; OPUS is cut while SONNET passes at the reserve boundary,
  observed through the Router's chosen model id; a denylisted desktop action never reaches
  the Runner; ASK→DENY never reaches the Runner; the daemon shares one Guard across reasoner
  / desktop skill / Control API; and both exhaustion-is-audible paths.
- **Deliberately NOT done:** `edith/voice/__main__.py` builds its own Router for the
  voice-only demo entry point and keeps the permissive defaults — it is a separate process,
  not the daemon, and giving it a Guard would be a second window with no Control API to read
  it. Guard itself is untouched and stays pure — no I/O, no bus, no model calls.
- **Owner LIVE-SMOKE still pending:** a real spoken denylisted action being refused aloud,
  and watching `budget_used` climb in the menu bar over a live session.
