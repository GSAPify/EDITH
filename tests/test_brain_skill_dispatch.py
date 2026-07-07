"""Brain skill dispatch (spec 02 §Skill contract §Brain dispatch path).

When an utterance matches a registered skill's trigger, Brain runs the skill,
publishes ``skill.result``, and SKIPS the recall→answer path (no model_call). No
match -> the existing answer path is unchanged. Empty registry (the default) is
exactly the pre-skill behaviour, so every existing brain test stays green.

Fake idiom mirrors tests/test_brain_resolve_hook.py.
"""

from __future__ import annotations

from edith.brain import Brain
from edith.bus import Event, EventBus
from edith.router import ModelResponse, Tier
from edith.skills.base import SkillContext, SkillResult


class FakeRouter:
    def __init__(self, answer: str = "done") -> None:
        self.answer = answer
        self.calls: list[tuple[list[dict[str, object]], Tier]] = []

    async def model_call(
        self, messages: list[dict[str, object]], tier_hint: Tier, max_tokens: int = 1024
    ) -> ModelResponse:
        self.calls.append((messages, tier_hint))
        return ModelResponse(text=self.answer, input_tokens=1, output_tokens=1)


class FakeMemory:
    def __init__(self) -> None:
        self.remembered_nodes: list[object] = []

    def recall(self, query: str) -> list[dict[str, object]]:
        return []

    def remember(self, nodes=None, edges=None) -> None:  # noqa: ANN001
        self.remembered_nodes.extend(nodes or [])


class FakeSkill:
    name = "fake-skill"
    triggers = ["review"]
    needs_confirmation = True

    def __init__(self) -> None:
        self.ran_with: list[str] = []

    async def run(self, context: SkillContext) -> SkillResult:
        self.ran_with.append(context.utterance)
        return SkillResult(skill=self.name, findings="looks good", pr_url="u", remembered=True)


async def _run(bus: EventBus, text: str) -> tuple[list[Event], list[Event]]:
    decisions: list[Event] = []
    skill_results: list[Event] = []

    async def cap_decision(event: Event) -> None:
        decisions.append(event)

    async def cap_skill(event: Event) -> None:
        skill_results.append(event)

    bus.subscribe("brain.decision", cap_decision)
    bus.subscribe("skill.result", cap_skill)
    await bus.publish("voice.utterance", source="voice", payload={"text": text})
    return decisions, skill_results


async def test_matching_utterance_dispatches_to_skill() -> None:
    bus = EventBus()
    router = FakeRouter()
    skill = FakeSkill()
    Brain(bus=bus, memory=FakeMemory(), router=router, skills=[skill])

    decisions, skill_results = await _run(bus, "review Tavishi's PR")

    assert skill.ran_with == ["review Tavishi's PR"]  # skill.run was called
    assert len(skill_results) == 1                     # skill.result published
    assert skill_results[0].source == "fake-skill"
    assert skill_results[0].payload["findings"] == "looks good"
    assert router.calls == []                          # answer path NOT taken
    assert decisions == []                             # no brain.decision


async def test_no_match_runs_answer_path() -> None:
    bus = EventBus()
    router = FakeRouter(answer="ok")
    skill = FakeSkill()
    Brain(bus=bus, memory=FakeMemory(), router=router, skills=[skill])

    decisions, skill_results = await _run(bus, "what is the weather")

    assert skill.ran_with == []          # no trigger match
    assert skill_results == []
    assert len(router.calls) == 1        # existing answer path ran
    assert decisions[0].payload["answer"] == "ok"


async def test_empty_registry_default_is_unchanged() -> None:
    bus = EventBus()
    router = FakeRouter(answer="ok")
    Brain(bus=bus, memory=FakeMemory(), router=router)  # no skills arg

    decisions, skill_results = await _run(bus, "review Tavishi's PR")

    assert skill_results == []
    assert len(router.calls) == 1
    assert decisions[0].payload["answer"] == "ok"
