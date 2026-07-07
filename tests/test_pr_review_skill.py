"""PRReviewSkill — the 7-step canonical flow (spec 02).

All deps are injected (Router, gh runner, Memory, confirm, speak) so the whole
flow runs offline. The crux is the confirm gate: the ``gh pr review`` write is
UNREACHABLE unless confirm returned True (test_declined_never_posts). The
planted-secret test proves the diff is redacted before it reaches the Router.

Fake idiom mirrors tests/test_brain_resolve_hook.py.
"""

from __future__ import annotations

import json

from edith.router import ModelResponse, Tier
from edith.skills import PRReviewSkill
from edith.skills.base import SkillContext


class FakeRouter:
    def __init__(self, answer: str = "LGTM with nits.") -> None:
        self.answer = answer
        self.calls: list[tuple[list[dict[str, object]], Tier]] = []

    async def model_call(
        self, messages: list[dict[str, object]], tier_hint: Tier, max_tokens: int = 1024
    ) -> ModelResponse:
        self.calls.append((messages, tier_hint))
        return ModelResponse(text=self.answer, input_tokens=1, output_tokens=1)


class FakeMemory:
    def __init__(self, recall_hits: list[dict[str, object]] | None = None) -> None:
        self.recall_hits = recall_hits or []
        self.remembered_nodes: list[object] = []
        self.remembered_edges: list[object] = []

    def recall(self, query: str) -> list[dict[str, object]]:
        return self.recall_hits

    def remember(self, nodes=None, edges=None) -> None:  # noqa: ANN001
        self.remembered_nodes.extend(nodes or [])
        self.remembered_edges.extend(edges or [])


class FakeGh:
    """Records every arg-list; returns canned stdout keyed on the subcommand."""

    def __init__(self, responses: dict[str, str]) -> None:
        self.responses = responses
        self.calls: list[list[str]] = []

    async def __call__(self, args: list[str]) -> str:
        self.calls.append(args)
        # key on the gh subcommand pair, e.g. "pr list" / "pr diff" / "pr view" / "pr review"
        key = " ".join(args[:2])
        return self.responses.get(key, "")


def _person_hit(name: str = "Tavishi", handle: str = "tavishi-gh") -> dict[str, object]:
    return {"label": "Person", "id": "person-tavishi", "name": name, "gh_handle": handle}


def _repo_hit(name: str = "edith") -> dict[str, object]:
    return {"label": "Repo", "id": "repo-edith", "name": name}


def _pr_list_json(prs: list[dict[str, object]]) -> str:
    return json.dumps(prs)


async def _confirm_true(_prompt: str) -> bool:
    return True


async def _confirm_false(_prompt: str) -> bool:
    return False


def _make_speak() -> tuple[list[str], object]:
    spoken: list[str] = []

    async def speak(text: str) -> None:
        spoken.append(text)

    return spoken, speak


def _ctx(memory: FakeMemory, utterance: str = "review Tavishi's PR") -> SkillContext:
    return SkillContext(utterance=utterance, memory=memory)


async def test_happy_path_confirm_true_posts() -> None:
    memory = FakeMemory(recall_hits=[_person_hit(), _repo_hit()])
    gh = FakeGh(
        {
            "pr list": _pr_list_json(
                [{"number": 12, "title": "Add auth", "url": "https://gh/pr/12"}]
            ),
            "pr diff": "diff --git a b\n+clean line",
            "pr view": json.dumps(
                {"title": "Add auth", "body": "desc", "url": "https://gh/pr/12"}
            ),
            "pr review": "",
        }
    )
    router = FakeRouter()
    spoken, speak = _make_speak()
    skill = PRReviewSkill(router, gh=gh, confirm=_confirm_true, speak=speak)

    result = await skill.run(_ctx(memory))

    assert result.posted is True
    assert result.findings == "LGTM with nits."
    assert result.pr_url == "https://gh/pr/12"
    # the gh pr review write WAS reached
    review_calls = [c for c in gh.calls if c[:2] == ["pr", "review"]]
    assert len(review_calls) == 1
    assert "--comment" in review_calls[0]
    assert len(router.calls) == 1  # exactly one opus review call
    assert router.calls[0][1] is Tier.OPUS


async def test_declined_never_posts() -> None:
    """THE crux: confirm -> False means the gh pr review subprocess is NEVER called."""
    memory = FakeMemory(recall_hits=[_person_hit(), _repo_hit()])
    gh = FakeGh(
        {
            "pr list": _pr_list_json(
                [{"number": 12, "title": "Add auth", "url": "https://gh/pr/12"}]
            ),
            "pr diff": "diff\n+line",
            "pr view": json.dumps({"title": "Add auth", "body": "d", "url": "u"}),
        }
    )
    router = FakeRouter()
    _spoken, speak = _make_speak()
    # default confirm is _deny; pass it explicitly to be unambiguous
    skill = PRReviewSkill(router, gh=gh, confirm=_confirm_false, speak=speak)

    result = await skill.run(_ctx(memory))

    assert result.posted is False
    review_calls = [c for c in gh.calls if c[:2] == ["pr", "review"]]
    assert review_calls == []  # the write was UNREACHABLE
    assert result.remembered is True  # still remembered


async def test_default_confirm_denies() -> None:
    """No confirm injected -> the _deny default -> never posts."""
    memory = FakeMemory(recall_hits=[_person_hit(), _repo_hit()])
    gh = FakeGh(
        {
            "pr list": _pr_list_json(
                [{"number": 7, "title": "Fix bug", "url": "https://gh/pr/7"}]
            ),
            "pr diff": "diff\n+x",
            "pr view": json.dumps({"title": "Fix bug", "body": "b", "url": "u"}),
        }
    )
    skill = PRReviewSkill(FakeRouter())  # all defaults: _deny, _silent, run_gh untouched

    result = await skill.run(_ctx(memory))

    assert result.posted is False
    assert [c for c in gh.calls if c[:2] == ["pr", "review"]] == []


async def test_unknown_person_asks_and_makes_no_calls() -> None:
    memory = FakeMemory(recall_hits=[])  # miss
    gh = FakeGh({})
    router = FakeRouter()
    spoken, speak = _make_speak()
    skill = PRReviewSkill(router, gh=gh, confirm=_confirm_true, speak=speak)

    result = await skill.run(_ctx(memory))

    assert result.asked != ""
    assert "Tavishi" in result.asked
    assert gh.calls == []           # no gh calls
    assert router.calls == []       # no model call
    assert result.posted is False
    assert spoken == [result.asked]  # the question was spoken


async def test_person_without_gh_handle_asks() -> None:
    hit = {"label": "Person", "id": "person-tavishi", "name": "Tavishi"}  # no gh_handle
    memory = FakeMemory(recall_hits=[hit, _repo_hit()])
    gh = FakeGh({})
    router = FakeRouter()
    _spoken, speak = _make_speak()
    skill = PRReviewSkill(router, gh=gh, confirm=_confirm_true, speak=speak)

    result = await skill.run(_ctx(memory))

    assert result.asked != ""
    assert gh.calls == []
    assert router.calls == []


async def test_multiple_open_prs_asks_which_one() -> None:
    memory = FakeMemory(recall_hits=[_person_hit(), _repo_hit()])
    gh = FakeGh(
        {
            "pr list": _pr_list_json(
                [
                    {"number": 12, "title": "auth", "url": "u12"},
                    {"number": 15, "title": "fix", "url": "u15"},
                    {"number": 17, "title": "docs", "url": "u17"},
                ]
            ),
        }
    )
    router = FakeRouter()
    _spoken, speak = _make_speak()
    skill = PRReviewSkill(router, gh=gh, confirm=_confirm_true, speak=speak)

    result = await skill.run(_ctx(memory))

    assert result.asked != ""
    assert "12" in result.asked and "15" in result.asked and "17" in result.asked
    assert router.calls == []  # no review posted, no opus call
    assert [c for c in gh.calls if c[:2] == ["pr", "review"]] == []


async def test_zero_open_prs_asks() -> None:
    memory = FakeMemory(recall_hits=[_person_hit(), _repo_hit()])
    gh = FakeGh({"pr list": _pr_list_json([])})
    router = FakeRouter()
    _spoken, speak = _make_speak()
    skill = PRReviewSkill(router, gh=gh, confirm=_confirm_true, speak=speak)

    result = await skill.run(_ctx(memory))

    assert result.asked != ""
    assert router.calls == []


async def test_planted_secret_redacted_before_router() -> None:
    """The diff carries live-shaped secrets; they must NOT reach the Router."""
    secret_diff = (
        "diff --git a/config.py b/config.py\n"
        "+api_key = sk-bf-DEADBEEF\n"
        "+client_secret = GOCSPX-xxxxSECRETxxxx\n"
    )
    memory = FakeMemory(recall_hits=[_person_hit(), _repo_hit()])
    gh = FakeGh(
        {
            "pr list": _pr_list_json(
                [{"number": 3, "title": "cfg", "url": "https://gh/pr/3"}]
            ),
            "pr diff": secret_diff,
            "pr view": json.dumps({"title": "cfg", "body": "b", "url": "u"}),
        }
    )
    router = FakeRouter()
    _spoken, speak = _make_speak()
    skill = PRReviewSkill(router, gh=gh, confirm=_confirm_false, speak=speak)

    await skill.run(_ctx(memory))

    # non-vacuous: the secrets WERE present in the raw diff
    assert "sk-bf-DEADBEEF" in secret_diff
    assert "GOCSPX-xxxxSECRETxxxx" in secret_diff
    # ...and must NOT appear in anything handed to the Router
    assert router.calls, "the review model_call must have fired"
    blob = " ".join(
        str(m.get("content", "")) for m in router.calls[0][0]
    )
    assert "sk-bf-DEADBEEF" not in blob
    assert "GOCSPX-xxxxSECRETxxxx" not in blob


async def test_remembers_even_when_not_posted() -> None:
    memory = FakeMemory(recall_hits=[_person_hit(), _repo_hit()])
    gh = FakeGh(
        {
            "pr list": _pr_list_json(
                [{"number": 9, "title": "T", "url": "https://gh/pr/9"}]
            ),
            "pr diff": "diff\n+x",
            "pr view": json.dumps({"title": "T", "body": "b", "url": "u"}),
        }
    )
    skill = PRReviewSkill(FakeRouter(), gh=gh, confirm=_confirm_false)

    result = await skill.run(_ctx(memory))

    assert result.remembered is True
    labels = {getattr(n, "label", None) for n in memory.remembered_nodes}
    assert "Person" in labels
    assert "PR" in labels
    assert "Fact" in labels
    # the Person node carries gh_handle so next time is a HIT
    person = next(n for n in memory.remembered_nodes if getattr(n, "label", None) == "Person")
    assert person.props.get("gh_handle") == "tavishi-gh"
