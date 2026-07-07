"""PRReviewSkill — the first end-to-end autonomous action (spec 02).

Given "review Tavishi's PR" it: resolves the person from Memory, locates their
open PR via ``gh``, fetches the diff, redacts it, routes a deep review to opus,
surfaces the findings, and — only after the owner CONFIRMS — posts the review to
GitHub. It remembers the exchange either way so the next invocation is faster.

Every dependency is constructor-injected (Router, gh runner, confirm, speak) so
the whole flow runs offline in tests. The confirm gate is the crux: the
``gh pr review`` write lives inside a single guarded branch that is unreachable
unless ``confirm`` returned True (spec 02 §Autonomy — that write is ASK).

The review rubric is inlined here; reusing OMC's ``/code-review`` rubric is a
documented follow-up (spec 02 §Open questions).
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable

from edith.ingest.extract import RouterLike
from edith.memory.secrets import sanitize_text
from edith.memory.store import Edge, Node
from edith.router import Tier
from edith.skills.base import SkillContext, SkillResult
from edith.skills.gh import GhRunner, run_gh

# Owner-confirm gate: prompt -> True to proceed with a shared-state write.
Confirm = Callable[[str], Awaitable[bool]]
# Speak out to the owner (voice / text surface).
Speak = Callable[[str], Awaitable[None]]

_REVIEW_MAX_TOKENS = 2048

# A capitalized name token after "review"/"look at"/"check", optionally possessive
# ("review Tavishi's PR", "check Nate PR"). A thin heuristic, not an NLP layer.
_NAME = re.compile(
    r"(?:review|look at|check(?:\s+out)?)\s+([A-Z][a-zA-Z]+)(?:'s)?",
)

_REVIEW_SYSTEM = (
    "You are a senior engineer reviewing a pull request diff. Give a concise, "
    "high-signal review: call out correctness bugs, security issues, and missing "
    "edge cases first; then note style or clarity nits briefly. Prefer a few "
    "important findings over an exhaustive list. If the diff looks clean, say so "
    "plainly. Reference file and line where useful."
)


async def _deny(_prompt: str) -> bool:
    """Default confirm: deny. A shared-state write never happens by default."""
    return False


async def _silent(_text: str) -> None:
    """Default speak: no-op."""


class PRReviewSkill:
    """Resolve → locate → fetch → review → surface → confirm → remember."""

    name = "pr-review"
    triggers = ["review", "check pr", "look at pr", "pull request"]
    needs_confirmation = True

    def __init__(
        self,
        router: RouterLike,
        *,
        gh: GhRunner = run_gh,
        confirm: Confirm = _deny,
        speak: Speak = _silent,
        org: str = "patterninc",
    ) -> None:
        self._router = router
        self._gh = gh
        self._confirm = confirm
        self._speak = speak
        self._org = org

    async def run(self, context: SkillContext) -> SkillResult:
        # STEP 1 — resolve the person from Memory (name -> gh_handle + one repo).
        name = self._extract_name(context.utterance)
        resolved = self._resolve_person(context, name)
        if resolved.asked:
            await self._speak(resolved.asked)
            return SkillResult(skill=self.name, asked=resolved.asked)
        handle, repo = resolved.handle, resolved.repo
        slug = f"{self._org}/{repo}"

        # STEP 2 — locate the open PR authored by that handle.
        prs = await self._list_prs(slug, handle)
        if len(prs) != 1:
            question = self._pr_ask(name, prs)
            await self._speak(question)
            return SkillResult(skill=self.name, asked=question)
        pr = prs[0]
        number = int(str(pr["number"]))
        pr_url = str(pr.get("url", ""))

        # STEP 3 — fetch the diff + PR context.
        diff = await self._gh(["pr", "diff", str(number), "--repo", slug])

        # STEP 4 — review. Ack now (masks the opus latency), redact BEFORE the
        # model sees anything, then one opus call.
        await self._speak(f"Fetching {name}'s PR, reviewing now…")
        safe_diff = sanitize_text(diff)
        messages: list[dict[str, object]] = [
            {"role": "system", "content": _REVIEW_SYSTEM},
            {"role": "user", "content": f"PR #{number} in {slug}\n\n{safe_diff}"},
        ]
        response = await self._router.model_call(
            messages, Tier.OPUS, max_tokens=_REVIEW_MAX_TOKENS
        )
        findings = response.text

        # STEP 5 — surface the findings to the owner.
        await self._speak(f"Reviewed {name}'s PR #{number}. {findings}")

        # STEP 6 — CONFIRM GATE. The gh pr review write is reachable ONLY here.
        posted = False
        if await self._confirm("Should I post this review on GitHub?"):
            await self._gh(
                ["pr", "review", str(number), "--repo", slug, "--comment", "--body", findings]
            )
            posted = True

        # STEP 7 — remember, whether or not it was posted.
        self._remember(context, name, handle, repo, number, pr, findings, posted)

        return SkillResult(
            skill=self.name,
            findings=findings,
            pr_url=pr_url,
            posted=posted,
            remembered=True,
        )

    @staticmethod
    def _extract_name(utterance: str) -> str:
        match = _NAME.search(utterance)
        return match.group(1) if match else ""

    def _resolve_person(self, context: SkillContext, name: str) -> _Resolved:
        """Person needs a match with a ``gh_handle`` AND exactly one repo."""
        if not name:
            return _Resolved(asked=self._person_ask(name))
        hits = context.memory.recall(name)
        people = [h for h in hits if h.get("label") == "Person"]
        repos = [h for h in hits if h.get("label") == "Repo"]
        person = people[0] if people else None
        handle = str(person.get("gh_handle", "")) if person else ""
        if person is None or not handle:
            return _Resolved(asked=self._person_ask(name))
        if len(repos) != 1:
            return _Resolved(asked=self._repo_ask(name, repos))
        return _Resolved(handle=handle, repo=str(repos[0].get("name", "")))

    async def _list_prs(self, slug: str, handle: str) -> list[dict[str, object]]:
        raw = await self._gh(
            [
                "pr", "list", "--repo", slug, "--author", handle,
                "--state", "open", "--json", "number,title,url",
            ]
        )
        raw = raw.strip()
        if not raw:
            return []
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, list) else []

    def _remember(  # noqa: PLR0913 - one call site; a params object would be over-engineering
        self,
        context: SkillContext,
        name: str,
        handle: str,
        repo: str,
        number: int,
        pr: dict[str, object],
        findings: str,
        posted: bool,
    ) -> None:
        person_id = f"person-{name.strip().lower().replace(' ', '-')}"
        pr_id = f"pr-{repo}-{number}"
        fact_id = f"fact-review-{repo}-{number}"
        summary = sanitize_text(f"Review of PR #{number} in {repo}: {findings}")
        nodes = [
            Node("Person", person_id, {"name": name, "gh_handle": handle}),
            Node(
                "PR",
                pr_id,
                {"number": number, "title": str(pr.get("title", "")), "state": "open"},
            ),
            Node("Fact", fact_id, {"text": summary, "source": "pr-review", "learned_at": ""}),
        ]
        edges: list[object] = [
            Edge("reviewed_by", "PR", pr_id, "Person", person_id),
            Edge("relates_to", "Fact", fact_id, "PR", pr_id),
        ]
        context.memory.remember(nodes=nodes, edges=edges)

    def _person_ask(self, name: str) -> str:
        who = name or "them"
        return f"I don't have {who} in my contacts — what's their GitHub handle?"

    def _repo_ask(self, name: str, repos: list[dict[str, object]]) -> str:
        names = ", ".join(f"`{r.get('name', '')}`" for r in repos)
        return f"{name} has open PRs in {names} — which one?"

    def _pr_ask(self, name: str, prs: list[dict[str, object]]) -> str:
        if not prs:
            return f"I couldn't find an open PR from {name} — which repo or PR should I look at?"
        listing = ", ".join(f"#{p['number']} ({p['title']})" for p in prs)
        return f"Found {len(prs)} open PRs from {name} — {listing}. Which?"


class _Resolved:
    """Outcome of person resolution: either a (handle, repo) pair or a question."""

    def __init__(self, *, handle: str = "", repo: str = "", asked: str = "") -> None:
        self.handle = handle
        self.repo = repo
        self.asked = asked
