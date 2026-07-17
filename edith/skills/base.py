"""The Skill contract (north-star §4.3, spec 02 §Skill contract).

A Skill is a capability with a ``name``, trigger phrases, a ``needs_confirmation``
flag, and an async ``run(context) -> result``. Brain matches an utterance against
each registered skill's triggers and dispatches to the first that matches.

``SkillContext`` carries the per-invocation input the skill needs; ``MemoryLike``
is the same read/write slice of the Memory contract Brain uses (brain/loop.py) —
declared locally so skills don't import Brain.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from edith.memory.store import Edge, Node


class MemoryLike(Protocol):
    """The slice of the Memory contract a Skill uses (north-star §4.3)."""

    def recall(self, query: str) -> list[dict[str, object]]: ...

    def remember(
        self, nodes: list[Node] | None = None, edges: list[Edge] | None = None
    ) -> None: ...


@dataclass
class SkillContext:
    """Per-invocation input handed to ``Skill.run``."""

    utterance: str
    memory: MemoryLike


@dataclass
class SkillResult:
    """What a Skill returns. ``asked`` is the clarifying question when the skill
    had to STOP and ask instead of acting (empty when it ran to completion).

    ``handled`` lets a skill DECLINE a turn its trigger matched: Brain dispatch skips
    a result with ``handled=False`` and falls through to the next skill / the answer
    loop. Defaults True so every existing skill is unaffected. This exists because
    broad triggers (desktop's "open "/"play ") can match an utterance the skill can't
    actually action — without it, that turn would dead-end instead of reaching the model.

    CONTRACT FOR SKILL AUTHORS: a skill that returns ``handled=False`` MUST NOT have
    acted — no OS side-effect, no ``speak``, no shared-state write — because Brain will
    run the next matching skill (and possibly the model) on the same utterance. Decide
    early: classify FIRST, then either act (``handled=True``) or decline untouched."""

    skill: str
    findings: str = ""
    pr_url: str = ""
    posted: bool = False
    remembered: bool = False
    asked: str = ""
    handled: bool = True


@runtime_checkable
class Skill(Protocol):
    """A dispatchable capability (spec 02 §Skill contract)."""

    name: str
    triggers: list[str]
    needs_confirmation: bool

    async def run(self, context: SkillContext) -> SkillResult: ...
