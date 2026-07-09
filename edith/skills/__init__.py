"""Skills — dispatchable capabilities (north-star §4.3, spec 02).

A Skill has a ``name``, trigger phrases, a ``needs_confirmation`` flag, and an
async ``run(context) -> result``. Brain matches an utterance against each
registered skill's triggers and dispatches to the first match. This package
ships the Skill contract, the injectable ``gh`` runner, and the first real
skill — ``PRReviewSkill`` (spec 02).
"""

from edith.skills.base import MemoryLike, Skill, SkillContext, SkillResult
from edith.skills.gh import GhError, GhRunner, run_gh
from edith.skills.pr_review import PRReviewSkill
from edith.skills.session_query import SessionQuerySkill

__all__ = [
    "GhError",
    "GhRunner",
    "MemoryLike",
    "PRReviewSkill",
    "SessionQuerySkill",
    "Skill",
    "SkillContext",
    "SkillResult",
    "run_gh",
]
