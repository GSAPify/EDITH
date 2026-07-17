"""Tests for Slice 6 — Desktop control (spec 06).

Headless: every OS side-effect goes through an injected ``runner`` seam that records
the argv it was handed and returns a canned ``(returncode, output)``. No subprocess,
no osascript, no ``open`` ever runs here. The real "open Spotify / start OMC" behaviour
is owner LIVE-SMOKE only (documented in the spec Completion Record).

Four surfaces:
  1. parse_command — regex fast-path classifies the top command shapes; misses -> None.
  2. RepoResolver — filesystem scan (two levels) + difflib; unique hit / ambiguous / miss.
  3. executors — the right argv / AppleScript string is constructed per action.
  4. DesktopControlSkill.run — parse -> resolve -> execute via the seam -> speak; and the
     dispatch-isolation guarantee (broad triggers don't steal a pr-review turn).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from edith.desktop import (
    AmbiguousRepo,
    DesktopAction,
    Intent,
    RepoNotFound,
    RepoResolver,
    launch_app,
    open_terminal,
    parse_command,
    spotify_command,
)
from edith.skills.base import SkillContext

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _RecordingRunner:
    """Records every argv it is handed; returns a canned (returncode, output)."""

    def __init__(self, returncode: int = 0, output: str = "") -> None:
        self.calls: list[list[str]] = []
        self._rc = returncode
        self._out = output

    async def __call__(self, argv: list[str]) -> tuple[int, str]:
        self.calls.append(argv)
        return self._rc, self._out


class _FakeMemory:
    """Minimal MemoryLike — desktop control never writes; recall returns nothing."""

    def recall(self, query: str) -> list[dict[str, object]]:
        return []

    def remember(self, nodes=None, edges=None) -> None:  # pragma: no cover - unused
        return None


# ---------------------------------------------------------------------------
# 1. parse_command — regex fast-path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("utterance", "expected"),
    [
        ("open Spotify", DesktopAction(intent=Intent.OPEN_APP, app="Spotify")),
        ("open Slack", DesktopAction(intent=Intent.OPEN_APP, app="Slack")),
        (
            "play Bohemian Rhapsody on Spotify",
            DesktopAction(intent=Intent.SPOTIFY, spotify_cmd="play", query="Bohemian Rhapsody"),
        ),
        ("pause the music", DesktopAction(intent=Intent.SPOTIFY, spotify_cmd="pause")),
        ("skip this song", DesktopAction(intent=Intent.SPOTIFY, spotify_cmd="next")),
        ("next track", DesktopAction(intent=Intent.SPOTIFY, spotify_cmd="next")),
        (
            "set the volume to 40",
            DesktopAction(intent=Intent.SPOTIFY, spotify_cmd="volume", volume=40),
        ),
        (
            "open a terminal in concorde_lib",
            DesktopAction(intent=Intent.TERMINAL, repo="concorde_lib"),
        ),
        (
            "start OMC in concorde_lib",
            DesktopAction(intent=Intent.OMC_LAUNCH, repo="concorde_lib"),
        ),
        (
            # hyphenated repo name must not truncate at the first hyphen (MEDIUM-3).
            "start OMC in brain-platform",
            DesktopAction(intent=Intent.OMC_LAUNCH, repo="brain-platform"),
        ),
        (
            "launch a terminal in the concorde_lib repo and start OMC",
            DesktopAction(intent=Intent.OMC_LAUNCH, repo="concorde_lib"),
        ),
        (
            # bare terminal, no repo -> a plain window (LOW-6).
            "open a terminal",
            DesktopAction(intent=Intent.TERMINAL, repo=None),
        ),
        (
            # volume clamped at parse so the spoken summary can't over-report (LOW-5).
            "set the volume to 500",
            DesktopAction(intent=Intent.SPOTIFY, spotify_cmd="volume", volume=100),
        ),
    ],
)
def test_parse_command_regex_hits(utterance: str, expected: DesktopAction) -> None:
    assert parse_command(utterance) == expected


@pytest.mark.parametrize(
    "utterance",
    [
        "what is the meaning of life",
        "review Tavishi's PR",
        "",
    ],
)
def test_parse_command_misses_return_none(utterance: str) -> None:
    assert parse_command(utterance) is None


# ---------------------------------------------------------------------------
# 2. RepoResolver — filesystem scan (two levels) + difflib
# ---------------------------------------------------------------------------


def _make_repo(root: Path, *parts: str) -> Path:
    """Create a fake git repo at root/parts.../  with a .git dir."""
    repo = root.joinpath(*parts)
    (repo / ".git").mkdir(parents=True)
    return repo


def test_resolver_exact_flat_match(tmp_path: Path) -> None:
    gitstuff = tmp_path / "gitstuff"
    _make_repo(gitstuff, "concorde_lib")
    _make_repo(gitstuff, "astro-brain")
    resolver = RepoResolver(roots=[gitstuff])
    assert resolver.resolve("concorde_lib") == gitstuff / "concorde_lib"


def test_resolver_scans_one_level_into_org_dirs(tmp_path: Path) -> None:
    """Repos cloned under org subdirs (~/gitstuff/patterninc/foo) resolve too."""
    gitstuff = tmp_path / "gitstuff"
    _make_repo(gitstuff, "patterninc", "brain-platform")
    resolver = RepoResolver(roots=[gitstuff])
    assert resolver.resolve("brain-platform") == gitstuff / "patterninc" / "brain-platform"


def test_resolver_fuzzy_close_match(tmp_path: Path) -> None:
    gitstuff = tmp_path / "gitstuff"
    _make_repo(gitstuff, "concorde_lib")
    resolver = RepoResolver(roots=[gitstuff])
    # "concorde" is a close, unambiguous match for "concorde_lib".
    assert resolver.resolve("concorde") == gitstuff / "concorde_lib"


def test_resolver_prefers_flat_over_nested(tmp_path: Path) -> None:
    """Flat ~/gitstuff/<name> + nested org copy = same repo cloned twice -> pick flat.

    The bulk workspace pull (clone_workspace.sh) clones into ~/gitstuff/<org>/, so a
    repo the owner also works on flat exists in both places with an identical remote.
    Prefer the shallow working copy rather than asking on every launch.
    """
    gitstuff = tmp_path / "gitstuff"
    _make_repo(gitstuff, "agents")
    _make_repo(gitstuff, "patterninc", "agents")
    resolver = RepoResolver(roots=[gitstuff])
    assert resolver.resolve("agents") == gitstuff / "agents"


def test_resolver_same_depth_collision_raises(tmp_path: Path) -> None:
    """Two org-nested copies at the SAME depth, no flat tiebreaker, must ASK."""
    gitstuff = tmp_path / "gitstuff"
    _make_repo(gitstuff, "patterninc", "agents")
    _make_repo(gitstuff, "ampmedia", "agents")
    resolver = RepoResolver(roots=[gitstuff])
    with pytest.raises(AmbiguousRepo) as excinfo:
        resolver.resolve("agents")
    assert len(excinfo.value.candidates) == 2


def test_resolver_miss_raises(tmp_path: Path) -> None:
    gitstuff = tmp_path / "gitstuff"
    _make_repo(gitstuff, "concorde_lib")
    resolver = RepoResolver(roots=[gitstuff])
    with pytest.raises(RepoNotFound):
        resolver.resolve("nonexistent_xyz")


# ---------------------------------------------------------------------------
# 3. executors — argv / AppleScript construction (no OS side-effects)
# ---------------------------------------------------------------------------


async def test_launch_app_builds_open_argv() -> None:
    runner = _RecordingRunner()
    await launch_app("Spotify", runner=runner)
    assert runner.calls == [["open", "-a", "Spotify"]]


async def test_spotify_play_builds_search_uri() -> None:
    runner = _RecordingRunner()
    await spotify_command("play", query="Bohemian Rhapsody", runner=runner)
    assert len(runner.calls) == 1
    argv = runner.calls[0]
    assert argv[0] == "osascript"
    script = argv[-1]
    assert 'application "Spotify"' in script
    assert "spotify:search:Bohemian Rhapsody" in script


async def test_spotify_pause_and_next() -> None:
    runner = _RecordingRunner()
    await spotify_command("pause", runner=runner)
    await spotify_command("next", runner=runner)
    assert "pause" in runner.calls[0][-1]
    assert "next track" in runner.calls[1][-1]


async def test_open_terminal_cds_to_path() -> None:
    runner = _RecordingRunner()
    await open_terminal(Path("/Users/akhil/gitstuff/concorde_lib"), runner=runner)
    script = runner.calls[0][-1]
    assert 'application "Terminal"' in script
    assert "do script" in script
    assert "cd" in script and "concorde_lib" in script


async def test_open_terminal_with_run_cmd_appends_claude() -> None:
    runner = _RecordingRunner()
    await open_terminal(
        Path("/Users/akhil/gitstuff/concorde_lib"), run_cmd="claude", runner=runner
    )
    script = runner.calls[0][-1]
    assert "claude" in script
    assert "&&" in script  # cd <path> && claude


# ---------------------------------------------------------------------------
# 4. DesktopControlSkill.run — end to end over the seam
# ---------------------------------------------------------------------------


def _skill(runner: _RecordingRunner, resolver: RepoResolver | None = None):
    from edith.skills.desktop_control import DesktopControlSkill

    spoken: list[str] = []

    async def _speak(text: str) -> None:
        spoken.append(text)

    skill = DesktopControlSkill(runner=runner, resolver=resolver, speak=_speak)
    return skill, spoken


async def test_skill_open_app_executes_and_speaks() -> None:
    runner = _RecordingRunner()
    skill, spoken = _skill(runner)
    result = await skill.run(SkillContext(utterance="open Spotify", memory=_FakeMemory()))
    assert runner.calls == [["open", "-a", "Spotify"]]
    assert result.skill == "desktop-control"
    assert spoken and "Spotify" in spoken[0]


async def test_skill_start_omc_resolves_repo_and_launches(tmp_path: Path) -> None:
    gitstuff = tmp_path / "gitstuff"
    _make_repo(gitstuff, "concorde_lib")
    resolver = RepoResolver(roots=[gitstuff])
    runner = _RecordingRunner()
    skill, spoken = _skill(runner, resolver=resolver)
    await skill.run(SkillContext(utterance="start OMC in concorde_lib", memory=_FakeMemory()))
    script = runner.calls[0][-1]
    assert "concorde_lib" in script
    assert "claude" in script  # OMC launch runs claude in the terminal


async def test_skill_ambiguous_repo_asks_does_not_execute(tmp_path: Path) -> None:
    gitstuff = tmp_path / "gitstuff"
    _make_repo(gitstuff, "patterninc", "agents")
    _make_repo(gitstuff, "ampmedia", "agents")
    resolver = RepoResolver(roots=[gitstuff])
    runner = _RecordingRunner()
    skill, spoken = _skill(runner, resolver=resolver)
    result = await skill.run(
        SkillContext(utterance="open a terminal in agents", memory=_FakeMemory())
    )
    assert runner.calls == []  # NOTHING launched on ambiguity
    assert result.asked  # it asked the owner to disambiguate


async def test_skill_needs_confirmation_is_false() -> None:
    from edith.skills.desktop_control import DesktopControlSkill

    assert DesktopControlSkill.needs_confirmation is False


async def test_skill_haiku_fallback_when_regex_misses() -> None:
    """A phrasing the regex can't classify falls back to a haiku JSON classify."""
    from edith.router import ModelResponse, Tier
    from edith.skills.desktop_control import DesktopControlSkill

    class _JsonRouter:
        def __init__(self) -> None:
            self.tiers: list[Tier] = []

        async def model_call(self, messages, tier_hint, max_tokens=1024):
            self.tiers.append(tier_hint)
            return ModelResponse(
                text='{"intent": "open_app", "app": "Notion"}', input_tokens=1, output_tokens=1
            )

    runner = _RecordingRunner()
    router = _JsonRouter()

    spoken: list[str] = []

    async def _speak(text: str) -> None:
        spoken.append(text)

    skill = DesktopControlSkill(runner=runner, router=router, speak=_speak)
    # "fire up my notes" has no regex hit -> haiku classifies it as open_app Notion.
    await skill.run(SkillContext(utterance="fire up my notes", memory=_FakeMemory()))
    assert runner.calls == [["open", "-a", "Notion"]]
    assert router.tiers == [Tier.HAIKU]  # cheapest tier, per spec


async def test_dispatch_isolation_pr_review_wins_over_desktop() -> None:
    """A pr-review utterance must not be stolen by desktop's broad triggers.

    Brain dispatches to the FIRST skill whose trigger matches; desktop is registered
    LAST. This asserts the registration order + trigger sets keep them disjoint on the
    utterances that matter.
    """
    from edith.skills.desktop_control import DesktopControlSkill
    from edith.skills.pr_review import PRReviewSkill

    class _StubRouter:
        async def model_call(self, messages, tier_hint, max_tokens=1024):  # pragma: no cover
            raise AssertionError("should not be called")

    pr = PRReviewSkill(_StubRouter())
    desktop = DesktopControlSkill(runner=_RecordingRunner())
    skills = [pr, desktop]  # desktop LAST, as edithd registers it

    def first_match(utterance: str):
        lowered = utterance.lower()
        return next(
            (s for s in skills if any(t.lower() in lowered for t in s.triggers)),
            None,
        )

    assert first_match("review Tavishi's PR") is pr
    assert first_match("open Spotify") is desktop
    assert first_match("play Bohemian Rhapsody on Spotify") is desktop


async def test_skill_speaks_correction_when_open_fails() -> None:
    """HIGH-1: a non-zero exit must NOT be reported as success."""
    runner = _RecordingRunner(returncode=1, output="Unable to find application")
    skill, spoken = _skill(runner)
    result = await skill.run(SkillContext(utterance="open Nonesuch", memory=_FakeMemory()))
    assert runner.calls == [["open", "-a", "Nonesuch"]]
    assert spoken and "couldn't open" in spoken[0].lower()
    assert "Opening Nonesuch." not in spoken  # never speaks false success
    assert result.findings and "couldn't" in result.findings.lower()


async def test_skill_declines_unparseable_turn_so_brain_falls_through() -> None:
    """MEDIUM-4: a trigger-matched utterance the parser can't action returns handled=False
    (no speak, no execution) so Brain continues to the answer loop instead of dead-ending.

    Bare "pause" matches the "pause" trigger but the parser needs a music noun after it
    ("pause the music"), so it classifies as None -> the skill declines the turn.
    """
    runner = _RecordingRunner()
    skill, spoken = _skill(runner)  # no router -> no haiku fallback
    assert parse_command("pause") is None  # precondition: bare "pause" doesn't parse
    result = await skill.run(SkillContext(utterance="pause", memory=_FakeMemory()))
    assert result.handled is False
    assert runner.calls == []  # nothing executed
    assert spoken == []  # and it stayed silent (Brain will answer instead)


async def test_skill_play_query_routes_through_shared_escaper() -> None:
    """MEDIUM-2: a query with a quote can't malform the AppleScript literal."""
    runner = _RecordingRunner()
    skill, _ = _skill(runner)
    await skill.run(
        SkillContext(utterance='play the song "1979"', memory=_FakeMemory())
    )
    script = runner.calls[0][-1]
    assert "spotify:search:" in script
    # the embedded quotes are escaped (\"), not stripped, and the literal stays balanced.
    assert script.count('"') % 2 == 0
