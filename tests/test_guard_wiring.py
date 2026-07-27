"""Guard wiring (spec 11 §Wiring) — the injection points, not Guard itself.

``tests/test_guard.py`` proves Guard's policy in isolation. This file proves the daemon
actually *uses* it, which is the whole difference between a budget and theatre:

  1. **The charge path.** Every ``Router.model_call*`` variant decrements a real Guard —
     non-streaming, streaming, and the two-call masked path (which must bill BOTH calls).
  2. **The tier reserve, observed through the Router.** At the reserve boundary an OPUS
     hint is cut to Sonnet while Sonnet itself still passes — the live voice degrades last.
  3. **The gate.** A denylisted desktop action is refused before any Runner call, and ASK
     is mapped to DENY (fail-closed) because no voice-confirm exists in the repo.
  4. **The daemon shares ONE Guard** across the reasoner, the desktop skill and the
     Control API's ``budget_used`` — a per-subsystem Guard would not be a budget.
  5. **Exhaustion is audible**: a budget-denied deep think says so instead of promising a
     ping that never comes, and the Narrator drops to its template instead of going quiet.

Fakes and ``httpx.MockTransport`` only — no live gateway, no audio, no real data dir.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from edith.brain import Brain
from edith.bus import Event, EventBus
from edith.daemon.client import ControlClient
from edith.daemon.edithd import EdithDaemon, Secrets
from edith.desktop import Intent
from edith.guard import Decision, Guard
from edith.router import BackgroundReasoner, JobStatus, ModelResponse, Router, Tier
from edith.session.narrator import Narrator
from edith.skills import DesktopControlSkill
from edith.skills.base import SkillContext

_BASE = "https://bifrost.test.internal/anthropic"
_KEY = "sk-bf-TESTKEY-not-real"
_MODELS = {
    Tier.HAIKU: "claude-haiku-4-5",
    Tier.SONNET: "claude-sonnet-4-6",
    Tier.OPUS: "claude-opus-4-8",
}


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _RecordingRunner:
    """Records every argv handed to it. If it is never called, nothing executed."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def __call__(self, argv: list[str]) -> tuple[int, str]:
        self.calls.append(argv)
        return 0, ""


class _Speaker:
    def __init__(self) -> None:
        self.said: list[str] = []

    async def __call__(self, text: str) -> None:
        self.said.append(text)


class _FakeMemory:
    def recall(self, query: str) -> list[dict[str, object]]:
        return []

    def remember(self, nodes=None, edges=None) -> None:  # noqa: ANN001
        return None


class _FakeRouter:
    def __init__(self, text: str = "ok") -> None:
        self.calls: list[tuple[list[dict[str, object]], Tier]] = []
        self._text = text

    async def model_call(
        self,
        messages: list[dict[str, object]],
        tier_hint: Tier,
        max_tokens: int = 1024,
    ) -> ModelResponse:
        self.calls.append((messages, tier_hint))
        return ModelResponse(text=self._text, input_tokens=1, output_tokens=1)


def _ok_body(text: str = "hi") -> dict[str, object]:
    return {
        "content": [{"type": "text", "text": text}],
        "usage": {"input_tokens": 7, "output_tokens": 3},
    }


def _event_lines(name: str, payload: dict[str, object]) -> list[str]:
    return [f"event: {name}", "data: " + json.dumps(payload), ""]


def _sse(*texts: str) -> bytes:
    """Anthropic-style SSE: input_tokens on message_start, output_tokens on message_delta."""
    lines = _event_lines(
        "message_start", {"type": "message_start", "message": {"usage": {"input_tokens": 5}}}
    )
    for t in texts:
        lines += _event_lines(
            "content_block_delta",
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": t}},
        )
    lines += _event_lines("message_delta", {"type": "message_delta", "usage": {"output_tokens": 3}})
    lines += _event_lines("message_stop", {"type": "message_stop"})
    return ("\n".join(lines)).encode()


def _guarded_router(handler, guard: Guard) -> Router:  # noqa: ANN001
    """A Router wired exactly the way the composition root wires it."""
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=_BASE)
    return Router(
        client,
        _KEY,
        _MODELS,
        budget_check=guard.budget_check,
        on_usage=guard.record,
    )


@pytest.fixture
def data_dir() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(dir="/tmp") as d:  # noqa: S108
        yield Path(d) / "edithdata"


# ---------------------------------------------------------------------------
# 1. The charge path — a budget that never decrements is theatre
# ---------------------------------------------------------------------------


async def test_model_call_charges_the_guard() -> None:
    async def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ok_body())

    guard = Guard()
    router = _guarded_router(handle, guard)
    assert guard.budget_used() == 0

    await router.model_call([{"role": "user", "content": "hi"}], Tier.SONNET)

    assert guard.budget_used() == 10  # 7 in + 3 out, from the response's usage block


async def test_model_call_stream_charges_the_guard() -> None:
    """Streaming IS chargeable: usage arrives on message_start + message_delta."""

    async def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=_sse("he", "llo"), headers={"content-type": "text/event-stream"}
        )

    guard = Guard()
    router = _guarded_router(handle, guard)

    stream = router.model_call_stream([{"role": "user", "content": "hi"}], Tier.HAIKU)
    chunks = [c async for c in stream]

    assert chunks[-1].is_final
    assert guard.budget_used() == 8  # 5 in (message_start) + 3 out (message_delta)


async def test_stream_charge_is_zero_not_guessed_when_the_gateway_omits_usage() -> None:
    """A gateway that reports no usage under-charges to 0 rather than inventing a number."""

    async def handle(request: httpx.Request) -> httpx.Response:
        lines = _event_lines(
            "content_block_delta",
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "x"}},
        )
        return httpx.Response(
            200, content=("\n".join(lines)).encode(), headers={"content-type": "text/event-stream"}
        )

    guard = Guard()
    router = _guarded_router(handle, guard)
    async for _chunk in router.model_call_stream([{"role": "user", "content": "hi"}], Tier.HAIKU):
        pass

    assert guard.budget_used() == 0


async def test_masked_charges_both_of_its_two_calls() -> None:
    """model_call_masked fires TWO billing events (ack stream + answer); both must land."""

    async def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("stream"):
            return httpx.Response(
                200, content=_sse("one moment"), headers={"content-type": "text/event-stream"}
            )
        return httpx.Response(200, json=_ok_body())

    guard = Guard()
    router = _guarded_router(handle, guard)

    ack_stream, answer_task = await router.model_call_masked(
        [{"role": "user", "content": "why?"}], ack_prompt="say one moment"
    )
    async for _chunk in ack_stream:
        pass
    await answer_task

    # 8 from the streamed ack (5+3) + 10 from the non-streaming answer (7+3).
    assert guard.budget_used() == 18


# ---------------------------------------------------------------------------
# 2. The tier reserve, observed through the Router
# ---------------------------------------------------------------------------


async def test_opus_is_cut_before_sonnet_at_the_reserve_boundary() -> None:
    """At 75% of the budget opus is refused and downgraded; sonnet still goes through."""
    seen: list[str] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content)["model"])
        return httpx.Response(200, json=_ok_body())

    # Budget 1000 → opus cap 750. Sit exactly on the boundary: opus denied, sonnet allowed.
    guard = Guard(token_budget=1000)
    guard.record(750, 0)
    assert guard.budget_check(Tier.OPUS) is False
    assert guard.budget_check(Tier.SONNET) is True

    router = _guarded_router(handle, guard)
    resp = await router.model_call([{"role": "user", "content": "deep"}], Tier.OPUS)

    assert resp.budget_limited is True
    assert seen == [_MODELS[Tier.SONNET]]  # the live voice keeps working on Sonnet


async def test_background_opus_is_denied_once_the_reserve_is_gone() -> None:
    """The reasoner's gate is Guard: a denied job never starts, so opus is never called."""
    router = _FakeRouter()
    guard = Guard(token_budget=1000)
    guard.record(800, 0)
    reasoner = BackgroundReasoner(router, budget_check=guard.budget_check)

    async def on_done(_resp: ModelResponse) -> None:  # pragma: no cover - never fires
        raise AssertionError("a denied job must not complete")

    job = await reasoner.think_async([{"role": "user", "content": "deep"}], on_done)

    assert job.status is JobStatus.DENIED
    assert job.task is None
    assert router.calls == []


# ---------------------------------------------------------------------------
# 3. The gate — a denylisted OS action is refused instead of executed
# ---------------------------------------------------------------------------


async def test_denylisted_desktop_action_does_not_execute() -> None:
    runner = _RecordingRunner()
    speaker = _Speaker()
    skill = DesktopControlSkill(
        runner=runner,
        speak=speaker,
        guard=Guard(denylist={Intent.OPEN_APP.value}),
    )

    result = await skill.run(SkillContext(utterance="open Spotify", memory=_FakeMemory()))

    assert runner.calls == []  # nothing ran
    assert result.handled is True  # refusing IS handling the turn
    assert speaker.said and "not permitted" in speaker.said[0]  # and she said why


async def test_ask_maps_to_deny_and_does_not_execute() -> None:
    """No voice-confirm exists in the repo, so ASK fails closed — never silently allowed."""
    runner = _RecordingRunner()
    speaker = _Speaker()
    guard = Guard(denylist=set())  # nothing denylisted: the ASK path is what's under test
    skill = DesktopControlSkill(runner=runner, speak=speaker, guard=guard)
    skill.needs_confirmation = True

    assert guard.authorize(Intent.OPEN_APP.value, needs_confirmation=True) is Decision.ASK

    result = await skill.run(SkillContext(utterance="open Spotify", memory=_FakeMemory()))

    assert runner.calls == []
    assert result.handled is True
    assert speaker.said and "haven't done it" in speaker.said[0]


async def test_default_denylist_leaves_desktop_actions_running() -> None:
    """Guard wired with its stock denylist must not change any existing behaviour."""
    runner = _RecordingRunner()
    skill = DesktopControlSkill(runner=runner, guard=Guard())

    await skill.run(SkillContext(utterance="open Spotify", memory=_FakeMemory()))

    assert runner.calls  # the action still executed


# ---------------------------------------------------------------------------
# 4. ONE Guard per daemon
# ---------------------------------------------------------------------------


async def test_daemon_shares_one_guard_across_its_seams(data_dir: Path) -> None:
    """The injected Guard reaches the reasoner, the desktop skill and the Control API."""
    guard = Guard(token_budget=1000)
    guard.record(900, 0)  # past the opus reserve, under the full cap
    daemon = EdithDaemon(
        data_dir=data_dir,
        secrets=Secrets(bifrost_api_key="k", bifrost_base_url="https://x"),
        memory=_FakeMemory(),
        router=_FakeRouter(),
        guard=guard,
    )
    await daemon.start()
    try:
        # Control API reports the real window usage, not a zero stub.
        reply = await ControlClient(daemon.socket_path).send({"cmd": "status"})
        assert reply["ok"] is True
        assert reply["status"]["budget_used"] == 900  # type: ignore[index]

        # The reasoner is gated by that same Guard.
        async def on_done(_resp: ModelResponse) -> None:  # pragma: no cover - never fires
            raise AssertionError("a denied job must not complete")

        assert daemon._reasoner is not None
        job = await daemon._reasoner.think_async([{"role": "user", "content": "x"}], on_done)
        assert job.status is JobStatus.DENIED

        # And so is the desktop skill.
        assert daemon._brain is not None
        desktop = [s for s in daemon._brain._skills if s.name == "desktop-control"]
        assert desktop and desktop[0]._guard is guard  # type: ignore[attr-defined]
    finally:
        await daemon.stop()


async def test_daemon_budget_used_tracks_spend_after_start(data_dir: Path) -> None:
    """A charge recorded after startup shows up on the Control API — one live window."""
    guard = Guard()
    daemon = EdithDaemon(
        data_dir=data_dir,
        secrets=Secrets(bifrost_api_key="k", bifrost_base_url="https://x"),
        memory=_FakeMemory(),
        router=_FakeRouter(),
        guard=guard,
    )
    await daemon.start()
    try:
        guard.record(40, 2)
        reply = await ControlClient(daemon.socket_path).send({"cmd": "status"})
        assert reply["status"]["budget_used"] == 42  # type: ignore[index]
    finally:
        await daemon.stop()


# ---------------------------------------------------------------------------
# 5. Exhaustion is audible, never a silent stop
# ---------------------------------------------------------------------------


async def test_denied_deep_think_says_so_instead_of_promising_a_ping() -> None:
    """Without this, the owner hears "I'll ping you" for a job that never started."""
    bus = EventBus()
    guard = Guard(token_budget=1000)
    guard.record(800, 0)  # opus reserve gone
    router = _FakeRouter()
    reasoner = BackgroundReasoner(router, budget_check=guard.budget_check)
    Brain(bus=bus, memory=_FakeMemory(), router=router, reasoner=reasoner)

    spoken: list[str] = []

    async def capture(event: Event) -> None:
        spoken.append(str(event.payload.get("answer", "")))

    bus.subscribe("brain.decision", capture)
    await bus.publish("voice.utterance", source="voice", payload={"text": "think about sharding"})

    assert spoken and "deep-thinking budget" in spoken[0]
    assert "ping you" not in spoken[0]


async def test_narrator_degrades_to_its_template_instead_of_going_quiet() -> None:
    """The Narrator calls at HAIKU; exhausted, it still speaks — just without a model call."""
    bus = EventBus()
    guard = Guard(token_budget=100)
    guard.record(100, 0)  # every tier is now out of budget
    router = _FakeRouter()
    speaker = _Speaker()
    # The exact binding edithd uses: zero-arg gate bound to the tier the Narrator calls at.
    Narrator(bus, speaker, router=router, budget_gate=lambda: guard.budget_check(Tier.HAIKU))

    await bus.publish(
        "session.event",
        source="session_bus",
        payload={"session_id": "s1", "kind": "error", "summary": "db timeout", "repo": "edith"},
    )

    assert router.calls == []  # no model call once the budget is gone
    assert speaker.said == ["Something errored in edith."]  # but she still speaks
