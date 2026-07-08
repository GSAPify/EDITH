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
    """Records speak() and set_paused() calls; zero audio deps."""

    def __init__(self) -> None:
        self.spoken: list[str] = []
        self.pause_states: list[bool] = []

    async def speak(self, text: str) -> None:
        self.spoken.append(text)

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
    """When voice= is wired, PRReviewSkill's speak seam is voice.speak.

    The unknown-person path calls speak(asked) immediately — no gh, no model
    call — so voice.spoken is populated after the utterance is dispatched.
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

    assert voice.spoken, "PRReviewSkill did not call voice.speak"


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
