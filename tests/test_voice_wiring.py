"""Tests for VoiceIO wiring into EdithDaemon (spec 03 §Wiring).

Fakes only — no real audio, no real model calls, no real Keychain.
TDD red→green: all tests drive changes in edith/daemon/edithd.py.

Three wiring assertions:
  1. With voice= wired, PRReviewSkill calls voice.speak on its speak seam
     (unknown-person path → speak(asked) fires immediately; no gh, no model).
  2. Control API pause → voice.set_paused(True).
  3. Control API resume (after pause) → voice.set_paused(False).
  4. voice=None (default) leaves all existing behaviour unchanged.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from edith.daemon.client import ControlClient
from edith.daemon.edithd import EdithDaemon, Secrets
from edith.router import ModelResponse, Tier

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeRouter:
    def __init__(self) -> None:
        self.calls: list[object] = []

    async def model_call(
        self,
        messages: list[dict[str, object]],
        tier_hint: Tier,
        max_tokens: int = 1024,
    ) -> ModelResponse:
        self.calls.append(messages)
        return ModelResponse(text="ok", input_tokens=1, output_tokens=1)


class SpyMemory:
    def recall(self, query: str) -> list[dict[str, object]]:
        return []

    def remember(self, nodes=None, edges=None) -> None:  # noqa: ANN001
        pass


class FakeVoiceIO:
    """Records speak() / speak_response() / set_paused() calls; zero audio deps.

    Tracks raw ``speak()`` and ``speak_response()`` separately so wiring tests can
    assert WHICH seam a caller used, not merely that something was said.
    """

    def __init__(self) -> None:
        self.spoken: list[str] = []
        self.response_spoken: list[str] = []
        self.pause_states: list[bool] = []

    async def speak(self, text: str) -> None:
        self.spoken.append(text)

    async def speak_response(self, text: str) -> None:
        self.response_spoken.append(text)

    def set_paused(self, paused: bool) -> None:
        self.pause_states.append(paused)


@pytest.fixture
def data_dir() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(dir="/tmp") as d:  # noqa: S108
        yield Path(d) / "edithdata"


def _daemon(data_dir: Path, voice: FakeVoiceIO | None = None) -> EdithDaemon:
    return EdithDaemon(
        data_dir=data_dir,
        secrets=Secrets(bifrost_api_key="k", bifrost_base_url="https://x"),
        memory=SpyMemory(),
        router=FakeRouter(),
        voice=voice,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_voice_speak_wired_into_pr_review_skill(data_dir: Path) -> None:
    """When voice= is wired, PRReviewSkill's speak seam is voice.speak_response.

    A user-triggered skill's acknowledgements/replies must re-arm follow-up
    independently, so it goes through speak_response — never raw speak. The
    unknown-person path calls speak(asked) immediately — no gh, no model call —
    so voice.response_spoken is populated after the utterance is dispatched.
    """
    voice = FakeVoiceIO()
    daemon = _daemon(data_dir, voice=voice)
    await daemon.start()
    try:
        await daemon.bus.publish(
            "voice.utterance",
            source="voice",
            payload={"text": "review Tavishi's PR"},
        )
    finally:
        await daemon.stop()

    assert voice.response_spoken, "PRReviewSkill did not call voice.speak_response"
    assert not voice.spoken, "PRReviewSkill must not use raw speak"


async def test_pause_calls_voice_set_paused_true(data_dir: Path) -> None:
    """Control API pause command → voice.set_paused(True)."""
    voice = FakeVoiceIO()
    daemon = _daemon(data_dir, voice=voice)
    await daemon.start()
    try:
        await ControlClient(daemon.socket_path).send({"cmd": "pause"})
    finally:
        await daemon.stop()

    assert True in voice.pause_states, "set_paused(True) not called after pause"


async def test_resume_calls_voice_set_paused_false(data_dir: Path) -> None:
    """Control API resume (after pause) → voice.set_paused(False)."""
    voice = FakeVoiceIO()
    daemon = _daemon(data_dir, voice=voice)
    await daemon.start()
    try:
        client = ControlClient(daemon.socket_path)
        await client.send({"cmd": "pause"})
        await client.send({"cmd": "resume"})
    finally:
        await daemon.stop()

    assert False in voice.pause_states, "set_paused(False) not called after resume"


async def test_plain_answer_is_spoken_via_brain_decision(data_dir: Path) -> None:
    """A non-skill utterance → Brain answers → daemon speaks brain.decision via
    speak_response (spec 10) — a plain answer re-arms follow-up just like a skill reply."""
    voice = FakeVoiceIO()
    daemon = _daemon(data_dir, voice=voice)
    await daemon.start()
    try:
        await daemon.bus.publish(
            "voice.utterance", source="voice", payload={"text": "what is the capital of France"}
        )
    finally:
        await daemon.stop()

    assert voice.response_spoken == ["ok"]  # the FakeRouter's answer, spoken exactly once
    assert not voice.spoken  # never the raw seam


async def test_skill_turn_does_not_double_speak(data_dir: Path) -> None:
    """A skill handles the turn and speaks itself; brain.decision must NOT also fire,
    so the router's plain answer ('ok') is never spoken on a skill turn (no double-speak)."""
    voice = FakeVoiceIO()
    daemon = _daemon(data_dir, voice=voice)
    await daemon.start()
    try:
        await daemon.bus.publish(
            "voice.utterance", source="voice", payload={"text": "review Tavishi's PR"}
        )
    finally:
        await daemon.stop()

    assert voice.response_spoken, "skill should have spoken (its asked/answer)"
    assert "ok" not in voice.response_spoken  # the plain-answer path did NOT also speak
    assert not voice.spoken  # nor via the raw seam


async def test_no_speak_the_decision_subscriber_without_voice(data_dir: Path) -> None:
    """voice=None: the plain-answer path just publishes brain.decision, nothing speaks."""
    daemon = _daemon(data_dir, voice=None)
    seen: list[object] = []

    async def cap(event: object) -> None:
        seen.append(event)

    await daemon.start()
    daemon.bus.subscribe("brain.decision", cap)
    try:
        await daemon.bus.publish(
            "voice.utterance", source="voice", payload={"text": "hello there"}
        )
    finally:
        await daemon.stop()

    assert len(seen) == 1  # brain.decision fired; no voice to speak it, no error


async def test_voice_none_leaves_behaviour_unchanged(data_dir: Path) -> None:
    """Default voice=None: daemon starts/stops cleanly, Control API works."""
    daemon = _daemon(data_dir, voice=None)
    await daemon.start()
    try:
        resp = await ControlClient(daemon.socket_path).send({"cmd": "status"})
    finally:
        await daemon.stop()

    assert resp["ok"] is True
    assert resp["status"]["state"] == "running"  # type: ignore[index]


async def test_session_narration_is_wired_to_raw_speak_not_response(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Session narration must go through voice.speak (raw), never voice.speak_response —
    an idle-narration transition must not steal a skill/reply's follow-up-arming signal."""
    import edith.daemon.edithd as edithd_module

    captured: dict[str, object] = {}
    real_narrator = edithd_module.Narrator

    class SpyNarrator(real_narrator):  # type: ignore[misc, valid-type]
        def __init__(self, bus, speak, **kwargs) -> None:  # noqa: ANN001, ANN003
            captured["speak"] = speak
            super().__init__(bus, speak, **kwargs)

    monkeypatch.setattr(edithd_module, "Narrator", SpyNarrator)

    voice = FakeVoiceIO()
    daemon = EdithDaemon(
        data_dir=data_dir,
        secrets=Secrets(bifrost_api_key="k", bifrost_base_url="https://x"),
        memory=SpyMemory(),
        router=FakeRouter(),
        voice=voice,
        enable_session_awareness=True,
    )
    await daemon.start()
    try:
        assert captured["speak"] == voice.speak
        assert captured["speak"] != voice.speak_response
    finally:
        await daemon.stop()
