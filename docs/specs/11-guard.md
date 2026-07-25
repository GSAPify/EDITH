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
