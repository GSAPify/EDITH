"""Guard — cross-cutting enforcement (north-star §6.2 budget, §6.3 autonomy gate).

Guard is the policy object the daemon consults before it acts or spends: the
**autonomy gate** (``authorize`` → allow/ask/deny) and the **token budget**
(``record`` / ``budget_check`` / ``budget_used``). It is deliberately pure and
headless — no I/O, no model calls, no bus. It decides and counts; the lead wires
its outputs into the Router (``budget_check`` seam + ``record`` after each call)
and the daemon (Control API ``budget_used``). Redaction, the third §6 duty, is
already owned by ``edith.memory.secrets.sanitize_text`` and lives in the Router's
outbound choke-point; Guard does not duplicate it.

``Tier`` is imported from ``edith.router.tiers`` (the tier-selection module) — the
same internal import ``bifrost.py`` uses, no cycle since ``tiers`` imports nothing
back.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from enum import Enum

from edith.router.tiers import Tier

# Out-of-the-box denylist: verbs EDITH must never auto-run, matching the owner's
# CLAUDE.md guardrails (no destructive shortcuts) and north-star §6.3's "deny"
# column. The lead/skills can override via the constructor. Exact-match verbs, not
# substrings — the caller passes a normalized action verb, not a raw shell string.
_DEFAULT_DENYLIST: frozenset[str] = frozenset(
    {"rm_rf", "drop_table", "force_push", "shutdown", "disk_wipe"}
)

# A daily token budget. 1,000,000 tokens/day is a generous-but-real ceiling for an
# always-on daemon on Pattern's Bifrost limits — enough headroom that normal use
# never trips it, low enough that a runaway narration loop is caught within a day.
# The exact number is a config knob, not load-bearing; the governance is.
_DEFAULT_TOKEN_BUDGET = 1_000_000

# A day, in seconds — the default budget window.
_DEFAULT_WINDOW_SECONDS = 86_400.0

# Fraction of the window budget OPUS is allowed to consume. OPUS costs more, so it
# is cut off before Sonnet: this reserves the budget tail for the live voice
# (Sonnet holds every turn) and starves only the expensive background/deep work
# first — mirroring how the Router consumes ``budget_check`` (an explicit OPUS hint
# that fails the check falls back to Sonnet with ``budget_limited=True``).
_OPUS_RESERVE_FRACTION = 0.75


class Decision(Enum):
    """The autonomy-gate verdict for an action (north-star §6.3)."""

    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class Guard:
    """Policy + counter: the autonomy gate and the per-window token budget.

    Pure and headless. Construct one per daemon; the lead injects its
    ``budget_check`` into the Router and reads ``budget_used`` in the Control API.
    """

    def __init__(
        self,
        denylist: set[str] | frozenset[str] | None = None,
        *,
        token_budget: int = _DEFAULT_TOKEN_BUDGET,
        window_seconds: float = _DEFAULT_WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._denylist: frozenset[str] = (
            frozenset(denylist) if denylist is not None else _DEFAULT_DENYLIST
        )
        self._token_budget = token_budget
        self._window_seconds = window_seconds
        self._clock = clock
        self._window_start: float = clock()
        self._used: int = 0

    # --- autonomy gate (§6.3) ---------------------------------------------

    def authorize(self, action: str, *, needs_confirmation: bool = False) -> Decision:
        """Decide whether ``action`` may run. Pure: no side effects.

        Precedence (DENY wins over ASK): a denylisted action is DENY even if it
        also asks for confirmation; otherwise ``needs_confirmation`` → ASK; else
        ALLOW. Membership is exact on a normalized action verb, not substring.
        """
        if action in self._denylist:
            return Decision.DENY
        if needs_confirmation:
            return Decision.ASK
        return Decision.ALLOW

    # --- token budget (§6.2) ----------------------------------------------

    def record(self, tokens_in: int, tokens_out: int) -> None:
        """Accumulate a model call's token cost into the current window.

        Rolls the window over first if the clock has passed it, so the cost lands
        in the correct window.
        """
        self._maybe_roll()
        self._used += tokens_in + tokens_out

    def budget_check(self, tier: Tier) -> bool:
        """True if a call at ``tier`` is still within budget for this window.

        Tier-aware: OPUS is capped at ``_OPUS_RESERVE_FRACTION`` of the budget so
        it is cut off before Sonnet/Haiku, reserving the tail for the live voice.
        """
        self._maybe_roll()
        cap = self._token_budget
        if tier is Tier.OPUS:
            cap = int(self._token_budget * _OPUS_RESERVE_FRACTION)
        return self._used < cap

    def budget_used(self) -> int:
        """Tokens used in the current window (0 after a rollover)."""
        self._maybe_roll()
        return self._used

    # --- window management ------------------------------------------------

    def _maybe_roll(self) -> None:
        """Reset usage when the clock has passed the current window.

        Sliding reset: the new window starts at ``now`` (not aligned to a fixed
        boundary). Called at the top of every public budget method so a pure read
        after the clock advances observes the reset without needing a ``record``.
        """
        now = self._clock()
        if now - self._window_start >= self._window_seconds:
            self._window_start = now
            self._used = 0
