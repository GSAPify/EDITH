"""SessionQuerySkill (spec 04 §Step 4): owner asks "what is session 2 doing?".

Routed through Brain's existing skill dispatch (phrase triggers, so it can't hijack
ordinary utterances). Read-only observation → needs_confirmation is False (§6.3 AUTO).
"""

from __future__ import annotations

import pytest

from edith.router import ModelResponse, Tier
from edith.skills import SessionQuerySkill
from edith.skills.base import SkillContext

pytestmark = pytest.mark.asyncio


class _Speaker:
    def __init__(self) -> None:
        self.said: list[str] = []

    async def __call__(self, text: str) -> None:
        self.said.append(text)


class _FakeRouter:
    def __init__(self) -> None:
        self.calls = []

    async def model_call(self, messages, tier_hint, max_tokens=512) -> ModelResponse:
        self.calls.append((messages, tier_hint))
        return ModelResponse(
            text="Session 2 is running tests in agents.", input_tokens=0, output_tokens=0
        )


class _FakeMemory:
    def recall(self, query):  # unused by this skill, present for SkillContext
        return []

    def remember(self, nodes=None, edges=None):
        pass


def _states() -> dict[str, dict]:
    return {
        "sess-one": {
            "session_id": "sess-one", "state": "working",
            "current_action": "using Bash", "repo": "seo-tools",
        },
        "sess-two": {
            "session_id": "sess-two", "state": "error",
            "current_action": "a tool call failed", "repo": "agents",
        },
    }


def _ctx(utterance: str) -> SkillContext:
    return SkillContext(utterance=utterance, memory=_FakeMemory())


async def test_is_read_only() -> None:
    skill = SessionQuerySkill(_states)
    assert skill.needs_confirmation is False
    assert any("session" in t for t in skill.triggers)


async def test_no_sessions_reports_none() -> None:
    spk = _Speaker()
    skill = SessionQuerySkill(dict, speak=spk)  # empty provider
    result = await skill.run(_ctx("what's running?"))
    assert "no active session" in result.findings.lower()
    assert spk.said and "no active session" in spk.said[0].lower()


async def test_summarizes_all_without_router() -> None:
    spk = _Speaker()
    skill = SessionQuerySkill(_states, speak=spk)
    result = await skill.run(_ctx("what are my sessions doing?"))
    blob = (result.findings + " " + " ".join(spk.said)).lower()
    assert "agents" in blob and "seo-tools" in blob
    assert "error" in blob  # state surfaced


async def test_uses_router_when_wired() -> None:
    spk = _Speaker()
    router = _FakeRouter()
    skill = SessionQuerySkill(_states, speak=spk, router=router)
    result = await skill.run(_ctx("what is session 2 doing?"))
    assert len(router.calls) == 1
    assert router.calls[0][1] is Tier.HAIKU
    assert result.findings == "Session 2 is running tests in agents."
    assert spk.said == ["Session 2 is running tests in agents."]


async def test_targets_a_specific_session_by_number() -> None:
    spk = _Speaker()
    router = _FakeRouter()
    skill = SessionQuerySkill(_states, speak=spk, router=router)
    await skill.run(_ctx("what is session 2 doing?"))
    # The model prompt should be scoped to the 2nd session (agents/error), not both.
    prompt_blob = str(router.calls[0][0])
    assert "agents" in prompt_blob
    assert "seo-tools" not in prompt_blob
