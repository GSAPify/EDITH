"""Guard — cross-cutting enforcement (north-star §6.2/§6.3). Pure policy + counter.

These tests pin the two contracts Guard owns as its own unit: the autonomy gate
(``authorize`` → allow/ask/deny with DENY winning over ASK) and the token budget
(accumulate within a window, tier-aware ``budget_check`` where OPUS is cut off
before Sonnet, and a deterministic window rollover driven by an injected clock).
"""

from __future__ import annotations

from edith.guard import Decision, Guard
from edith.router import Tier


class _FakeClock:
    """Injectable monotonic clock so window rollover is deterministic in tests."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# --- authorize (autonomy gate) --------------------------------------------


def test_authorize_allows_plain_action() -> None:
    guard = Guard()
    assert guard.authorize("read_repo") is Decision.ALLOW


def test_authorize_asks_when_confirmation_needed() -> None:
    guard = Guard()
    assert guard.authorize("open_app", needs_confirmation=True) is Decision.ASK


def test_authorize_denies_denylisted_action() -> None:
    guard = Guard(denylist={"rm_rf"})
    assert guard.authorize("rm_rf") is Decision.DENY


def test_authorize_deny_wins_over_ask() -> None:
    # A denylisted action that also wants confirmation is DENY, never ASK.
    guard = Guard(denylist={"drop_table"})
    assert guard.authorize("drop_table", needs_confirmation=True) is Decision.DENY


def test_authorize_uses_exact_membership_not_substring() -> None:
    # "rm" being denied must not deny "confirm" or "warm_cache".
    guard = Guard(denylist={"rm"})
    assert guard.authorize("confirm") is Decision.ALLOW
    assert guard.authorize("warm_cache") is Decision.ALLOW


def test_default_denylist_is_nonempty() -> None:
    # The out-of-the-box policy denies at least the destructive verbs it documents.
    guard = Guard()
    assert guard.authorize("shutdown") is Decision.DENY


# --- budget: accumulation + used ------------------------------------------


def test_record_accumulates_used() -> None:
    guard = Guard(token_budget=1000)
    assert guard.budget_used() == 0
    guard.record(tokens_in=100, tokens_out=50)
    guard.record(tokens_in=30, tokens_out=20)
    assert guard.budget_used() == 200


# --- budget: budget_check flips false past the cap ------------------------


def test_budget_check_true_within_cap() -> None:
    guard = Guard(token_budget=1000)
    guard.record(tokens_in=100, tokens_out=100)
    assert guard.budget_check(Tier.SONNET) is True
    assert guard.budget_check(Tier.HAIKU) is True


def test_budget_check_false_past_cap_for_sonnet() -> None:
    guard = Guard(token_budget=1000)
    guard.record(tokens_in=600, tokens_out=600)  # 1200 > 1000
    assert guard.budget_check(Tier.SONNET) is False


def test_opus_cut_off_before_sonnet() -> None:
    # The discriminating case: at the same usage level, OPUS is denied while
    # SONNET (the live voice) is still allowed — OPUS costs more, so it is
    # reserved out first. Uses the default 0.75 reserve fraction.
    guard = Guard(token_budget=1000)
    guard.record(tokens_in=500, tokens_out=300)  # 800 used: > 750 opus cap, < 1000
    assert guard.budget_check(Tier.OPUS) is False
    assert guard.budget_check(Tier.SONNET) is True
    assert guard.budget_check(Tier.HAIKU) is True


# --- budget: window rollover resets usage ---------------------------------


def test_window_rollover_resets_usage_on_read() -> None:
    clock = _FakeClock()
    guard = Guard(token_budget=1000, window_seconds=100.0, clock=clock)
    guard.record(tokens_in=400, tokens_out=400)  # 800 used
    assert guard.budget_used() == 800
    assert guard.budget_check(Tier.SONNET) is True

    # Advance past the window; a pure read must observe the reset (no record needed).
    clock.advance(101.0)
    assert guard.budget_used() == 0
    assert guard.budget_check(Tier.OPUS) is True


def test_window_does_not_roll_before_it_elapses() -> None:
    clock = _FakeClock()
    guard = Guard(token_budget=1000, window_seconds=100.0, clock=clock)
    guard.record(tokens_in=300, tokens_out=0)
    clock.advance(50.0)  # still inside the window
    guard.record(tokens_in=200, tokens_out=0)
    assert guard.budget_used() == 500


def test_record_after_rollover_starts_fresh_window() -> None:
    clock = _FakeClock()
    guard = Guard(token_budget=1000, window_seconds=100.0, clock=clock)
    guard.record(tokens_in=900, tokens_out=0)
    clock.advance(150.0)  # roll
    guard.record(tokens_in=100, tokens_out=0)  # counts against the fresh window
    assert guard.budget_used() == 100
