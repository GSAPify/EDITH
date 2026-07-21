"""Guard — cross-cutting enforcement (north-star §6): autonomy gate + token budget.

Pure policy + counter. See ``docs/specs/11-guard.md`` for the model and the two
lead wiring touchpoints (Router ``budget_check``/``record``, daemon ``budget_used``).
"""

from edith.guard.guard import Decision, Guard

__all__ = ["Decision", "Guard"]
