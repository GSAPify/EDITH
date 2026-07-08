"""TDD tests for ElevenLabsAdapter, PiperAdapter, and select_adapter (spec 03 §Tech choices).

All tests use injected fakes — no real audio, network, or subprocess calls.
Pattern: fake client/runner + fake sink → assert chunks flow and stop() cancels.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator

import pytest

from edith.voice.adapters import ElevenLabsAdapter, PiperAdapter, select_adapter
from edith.voice.tts import TTSHandle  # noqa: F401 — re-exported; used in isinstance checks

# ---------------------------------------------------------------------------
# Shared fakes
# ---------------------------------------------------------------------------


async def _fake_stream(api_key: str, voice_id: str, text: str) -> AsyncGenerator[bytes, None]:
    """Deterministic fake: yields two PCM chunks and returns."""
    yield b"chunk1"
    yield b"chunk2"


async def _slow_stream(api_key: str, voice_id: str, text: str) -> AsyncGenerator[bytes, None]:
    """Slow stream that blocks mid-way — used to prove stop() cancels early."""
    yield b"chunk1"
    await asyncio.sleep(100)  # Blocked until cancelled
    yield b"chunk2"  # Never reached if cancelled


class FakeSink:
    """Records received PCM chunks for assertion."""

    def __init__(self) -> None:
        self.received: list[bytes] = []

    def __call__(self, chunk: bytes) -> None:
        self.received.append(chunk)


class FakePiperProcess:
    """Fake Piper subprocess.

    Passing non-empty *pcm_data* pre-feeds a StreamReader with that data + EOF
    so ``_drain`` runs to completion immediately.  An empty (default) pcm_data
    leaves the reader blocking — useful for stop() / termination tests.
    """

    def __init__(self, pcm_data: bytes = b"") -> None:
        self.terminated = False
        reader: asyncio.StreamReader = asyncio.StreamReader()
        if pcm_data:
            reader.feed_data(pcm_data)
            reader.feed_eof()
        self.stdout: asyncio.StreamReader | None = reader

    def terminate(self) -> None:
        self.terminated = True


# ---------------------------------------------------------------------------
# ElevenLabsAdapter
# ---------------------------------------------------------------------------


async def test_elevenlabs_name() -> None:
    adapter = ElevenLabsAdapter(
        api_key="k", voice_id="v", stream_factory=_fake_stream, sink=FakeSink()
    )
    assert adapter.name() == "elevenlabs"


async def test_elevenlabs_chunks_flow_to_sink() -> None:
    """All PCM chunks yielded by the stream factory must reach the sink in order."""
    sink = FakeSink()
    adapter = ElevenLabsAdapter(api_key="k", voice_id="v", stream_factory=_fake_stream, sink=sink)
    handle = await adapter.speak("Awaiting your instructions.")
    await asyncio.sleep(0)  # One event-loop tick lets the background task run to completion
    assert sink.received == [b"chunk1", b"chunk2"]
    assert isinstance(handle, TTSHandle)


async def test_elevenlabs_speak_passes_text_to_factory() -> None:
    """speak(text) forwards the exact text string to the stream factory."""
    captured: list[str] = []

    async def _capturing_stream(
        api_key: str, voice_id: str, text: str
    ) -> AsyncGenerator[bytes, None]:
        captured.append(text)
        yield b"x"

    adapter = ElevenLabsAdapter(
        api_key="k", voice_id="v", stream_factory=_capturing_stream, sink=FakeSink()
    )
    await adapter.speak("hello world")
    await asyncio.sleep(0)
    assert captured == ["hello world"]


async def test_elevenlabs_stop_cancels_stream() -> None:
    """stop() cancels the background task; chunk2 from the slow stream is never delivered."""
    sink = FakeSink()
    adapter = ElevenLabsAdapter(api_key="k", voice_id="v", stream_factory=_slow_stream, sink=sink)
    handle = await adapter.speak("hello")
    await asyncio.sleep(0)  # Task starts; chunk1 delivered; now suspended at sleep(100)
    handle.stop()
    await asyncio.sleep(0)  # CancelledError propagates through task
    assert b"chunk2" not in sink.received  # Never reached after cancellation
    assert isinstance(handle, TTSHandle)


# ---------------------------------------------------------------------------
# PiperAdapter
# ---------------------------------------------------------------------------


async def test_piper_name() -> None:
    adapter = PiperAdapter()  # name() needs no runner
    assert adapter.name() == "piper"


async def test_piper_chunks_flow_to_sink() -> None:
    """PCM bytes read from the process stdout are forwarded to the sink."""
    sink = FakeSink()
    fake_proc = FakePiperProcess(b"pcm_bytes")

    async def _runner(args: list[str]) -> FakePiperProcess:
        return fake_proc

    adapter = PiperAdapter(runner=_runner, sink=sink)
    handle = await adapter.speak("hello")
    await asyncio.sleep(0)  # Let drain task empty the pre-fed reader
    assert sink.received == [b"pcm_bytes"]
    assert isinstance(handle, TTSHandle)


async def test_piper_stop_terminates_process() -> None:
    """stop() calls terminate() on the subprocess (blocking reader, no EOF)."""
    sink = FakeSink()
    fake_proc = FakePiperProcess()  # Blocking reader — task never finishes on its own

    async def _runner(args: list[str]) -> FakePiperProcess:
        return fake_proc

    adapter = PiperAdapter(runner=_runner, sink=sink)
    handle = await adapter.speak("hello")
    handle.stop()
    assert fake_proc.terminated


async def test_piper_runner_receives_arg_list() -> None:
    """Runner is called with a list of strings — never a shell string."""
    received: list[list[str]] = []
    fake_proc = FakePiperProcess(b"")

    async def _runner(args: list[str]) -> FakePiperProcess:
        received.append(list(args))
        return fake_proc

    adapter = PiperAdapter(runner=_runner, sink=FakeSink())
    await adapter.speak("hello world")

    assert len(received) == 1
    args = received[0]
    assert isinstance(args, list), "runner must receive a list, not a shell string"
    assert args[0] == "piper"
    assert "--output-raw" in args
    assert "hello world" in args


async def test_piper_model_path_included_when_set() -> None:
    """When model_path is provided it appears in the subprocess arg list."""
    received: list[list[str]] = []
    fake_proc = FakePiperProcess(b"")

    async def _runner(args: list[str]) -> FakePiperProcess:
        received.append(list(args))
        return fake_proc

    adapter = PiperAdapter(model_path="/models/en.onnx", runner=_runner, sink=FakeSink())
    await adapter.speak("test")

    args = received[0]
    assert "--model" in args
    assert "/models/en.onnx" in args


# ---------------------------------------------------------------------------
# select_adapter
# ---------------------------------------------------------------------------


def test_select_adapter_returns_elevenlabs() -> None:
    adapter = select_adapter(
        "elevenlabs",
        api_key="k",
        voice_id="v",
        stream_factory=_fake_stream,
        sink=FakeSink(),
    )
    assert isinstance(adapter, ElevenLabsAdapter)
    assert adapter.name() == "elevenlabs"


def test_select_adapter_returns_piper() -> None:
    adapter = select_adapter("piper")
    assert isinstance(adapter, PiperAdapter)
    assert adapter.name() == "piper"


def test_select_adapter_unknown_engine_raises_value_error() -> None:
    with pytest.raises(ValueError, match="Unknown"):
        select_adapter("google-tts")


def test_select_adapter_unknown_engine_message_includes_engine_name() -> None:
    with pytest.raises(ValueError, match="whisper"):
        select_adapter("whisper")
