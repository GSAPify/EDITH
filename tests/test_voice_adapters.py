"""TDD tests for ElevenLabsAdapter, PiperAdapter, and select_adapter (spec 03 §Tech choices).

All tests use injected fakes — no real audio, network, or subprocess calls.
Pattern: fake client/runner + fake sink → assert chunks flow and stop() cancels.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import threading
import types
from collections.abc import AsyncGenerator

import pytest

from edith.voice.adapters import (
    ElevenLabsAdapter,
    PiperAdapter,
    _ElevenLabsHandle,
    _PiperHandle,
    _RawOutputSink,
    resolve_device_override,
    resolve_output_device,
    select_adapter,
)
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


async def _erroring_stream(api_key: str, voice_id: str, text: str) -> AsyncGenerator[bytes, None]:
    """Stream that raises after one chunk — used to prove the sink still closes."""
    yield b"chunk1"
    raise RuntimeError("stream boom")


class FakeSink:
    """Records received PCM chunks for assertion."""

    def __init__(self) -> None:
        self.received: list[bytes] = []

    def __call__(self, chunk: bytes) -> None:
        self.received.append(chunk)


class FakeClosableSink:
    """A sink exposing ``close()`` — records chunks AND close() call count."""

    def __init__(self) -> None:
        self.received: list[bytes] = []
        self.close_calls = 0

    def __call__(self, chunk: bytes) -> None:
        self.received.append(chunk)

    def close(self) -> None:
        self.close_calls += 1


class FakeRawOutputStream:
    """Fake ``sounddevice.RawOutputStream`` — records ctor kwargs, no hardware."""

    instances: list[FakeRawOutputStream] = []

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.started = False
        self.stopped = False
        self.aborted = False
        self.closed = False
        self.stop_calls = 0
        self.abort_calls = 0
        self.close_calls = 0
        FakeRawOutputStream.instances.append(self)

    def start(self) -> None:
        self.started = True

    def write(self, chunk: bytes) -> None:
        pass

    def stop(self) -> None:
        self.stopped = True
        self.stop_calls += 1

    def abort(self) -> None:
        self.aborted = True
        self.abort_calls += 1

    def close(self) -> None:
        self.closed = True
        self.close_calls += 1


@pytest.fixture
def fake_sounddevice(monkeypatch: pytest.MonkeyPatch) -> type[FakeRawOutputStream]:
    """Install a fake ``sounddevice`` module so ``import sounddevice as sd`` inside the
    adapters' ``_default_sink()`` resolves to it — no real PortAudio call, no hardware."""
    FakeRawOutputStream.instances = []
    fake_module = types.SimpleNamespace(RawOutputStream=FakeRawOutputStream)
    monkeypatch.setitem(sys.modules, "sounddevice", fake_module)
    return FakeRawOutputStream


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
# _RawOutputSink — idempotent abort() vs close() (round 2 review: cancellation must
# discard queued audio via abort(), never drain via stop()).
# ---------------------------------------------------------------------------


def test_raw_output_sink_close_stops_and_closes_the_stream() -> None:
    stream = FakeRawOutputStream()
    _RawOutputSink(stream).close()
    assert stream.stop_calls == 1
    assert stream.abort_calls == 0
    assert stream.close_calls == 1


def test_raw_output_sink_abort_aborts_and_closes_the_stream() -> None:
    stream = FakeRawOutputStream()
    _RawOutputSink(stream).abort()
    assert stream.abort_calls == 1
    assert stream.stop_calls == 0
    assert stream.close_calls == 1


def test_raw_output_sink_close_then_abort_is_idempotent() -> None:
    """abort() after close() must not run the stream teardown a second time."""
    stream = FakeRawOutputStream()
    sink = _RawOutputSink(stream)
    sink.close()
    sink.abort()
    assert stream.stop_calls == 1
    assert stream.abort_calls == 0
    assert stream.close_calls == 1


def test_raw_output_sink_abort_then_close_is_idempotent() -> None:
    """close() after abort() must not run the stream teardown a second time."""
    stream = FakeRawOutputStream()
    sink = _RawOutputSink(stream)
    sink.abort()
    sink.close()
    assert stream.abort_calls == 1
    assert stream.stop_calls == 0
    assert stream.close_calls == 1


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


async def test_elevenlabs_closes_closable_sink_on_normal_completion() -> None:
    """A sink exposing close() is closed exactly once when the stream finishes normally."""
    sink = FakeClosableSink()
    adapter = ElevenLabsAdapter(api_key="k", voice_id="v", stream_factory=_fake_stream, sink=sink)
    await adapter.speak("hello")
    await asyncio.sleep(0)
    assert sink.close_calls == 1


async def test_elevenlabs_closes_closable_sink_on_stream_exception() -> None:
    """A stream error must still close the sink exactly once (never leaked)."""
    sink = FakeClosableSink()
    adapter = ElevenLabsAdapter(
        api_key="k", voice_id="v", stream_factory=_erroring_stream, sink=sink
    )
    handle = await adapter.speak("hello")
    await asyncio.sleep(0)
    with contextlib.suppress(RuntimeError):
        await handle._task  # type: ignore[attr-defined]  # noqa: SLF001 — retrieve exception
    assert sink.close_calls == 1


async def test_elevenlabs_closes_closable_sink_on_stop_cancellation() -> None:
    """handle.stop() cancels the task; the closable sink still closes exactly once.

    Awaits the task to completion rather than a fixed tick count: stop() schedules
    cancellation via call_soon_threadsafe (round 2 review), which takes one more
    event-loop iteration than a direct task.cancel() would.
    """
    sink = FakeClosableSink()
    adapter = ElevenLabsAdapter(api_key="k", voice_id="v", stream_factory=_slow_stream, sink=sink)
    handle = await adapter.speak("hello")
    await asyncio.sleep(0)
    handle.stop()
    with contextlib.suppress(asyncio.CancelledError):
        await handle._task  # type: ignore[attr-defined]  # noqa: SLF001
    assert sink.close_calls == 1


async def test_elevenlabs_default_sink_aborts_not_stops_on_cancellation(
    fake_sounddevice: type[FakeRawOutputStream],
) -> None:
    """Cancellation must discard queued audio via abort(), never drain via stop()."""
    adapter = ElevenLabsAdapter(api_key="k", voice_id="v", stream_factory=_slow_stream)
    handle = await adapter.speak("hello")
    await asyncio.sleep(0)
    handle.stop()
    with contextlib.suppress(asyncio.CancelledError):
        await handle._task  # type: ignore[attr-defined]  # noqa: SLF001
    stream = fake_sounddevice.instances[-1]
    assert stream.abort_calls == 1
    assert stream.stop_calls == 0
    assert stream.close_calls == 1


async def test_elevenlabs_default_sink_stops_not_aborts_on_normal_completion(
    fake_sounddevice: type[FakeRawOutputStream],
) -> None:
    adapter = ElevenLabsAdapter(api_key="k", voice_id="v", stream_factory=_fake_stream)
    await adapter.speak("hello")
    await asyncio.sleep(0)
    stream = fake_sounddevice.instances[-1]
    assert stream.stop_calls == 1
    assert stream.abort_calls == 0
    assert stream.close_calls == 1


async def test_elevenlabs_default_sink_aborts_not_stops_on_stream_exception(
    fake_sounddevice: type[FakeRawOutputStream],
) -> None:
    """A genuine stream error is also an abort-path teardown, not a drain."""
    adapter = ElevenLabsAdapter(api_key="k", voice_id="v", stream_factory=_erroring_stream)
    handle = await adapter.speak("hello")
    await asyncio.sleep(0)
    with contextlib.suppress(RuntimeError):
        await handle._task  # type: ignore[attr-defined]  # noqa: SLF001
    stream = fake_sounddevice.instances[-1]
    assert stream.abort_calls == 1
    assert stream.stop_calls == 0


class _RaisingAbortSink:
    """A sink whose abort() itself raises — used to prove teardown errors never
    replace the original cancellation/error (round 2 review)."""

    def __init__(self) -> None:
        self.received: list[bytes] = []
        self.abort_calls = 0

    def __call__(self, chunk: bytes) -> None:
        self.received.append(chunk)

    def abort(self) -> None:
        self.abort_calls += 1
        raise RuntimeError("teardown abort boom")


class _RaisingCloseSink:
    """A sink whose close() itself raises — same purpose as _RaisingAbortSink,
    but for a sink with no abort() (falls back to close() in _abort_sink)."""

    def __init__(self) -> None:
        self.received: list[bytes] = []
        self.close_calls = 0

    def __call__(self, chunk: bytes) -> None:
        self.received.append(chunk)

    def close(self) -> None:
        self.close_calls += 1
        raise RuntimeError("teardown close boom")


async def test_elevenlabs_teardown_exception_does_not_replace_cancelled_error() -> None:
    sink = _RaisingAbortSink()
    adapter = ElevenLabsAdapter(api_key="k", voice_id="v", stream_factory=_slow_stream, sink=sink)
    handle = await adapter.speak("hello")
    await asyncio.sleep(0)
    handle.stop()
    with pytest.raises(asyncio.CancelledError):
        await handle._task  # type: ignore[attr-defined]  # noqa: SLF001
    assert sink.abort_calls == 1


async def test_elevenlabs_teardown_exception_does_not_replace_stream_error() -> None:
    sink = _RaisingCloseSink()  # no abort() -> _abort_sink falls back to close()
    adapter = ElevenLabsAdapter(
        api_key="k", voice_id="v", stream_factory=_erroring_stream, sink=sink
    )
    handle = await adapter.speak("hello")
    await asyncio.sleep(0)
    with pytest.raises(RuntimeError, match="stream boom"):
        await handle._task  # type: ignore[attr-defined]  # noqa: SLF001
    assert sink.close_calls == 1


async def test_elevenlabs_handle_stop_from_another_thread_is_thread_safe() -> None:
    """stop() can run from the mic worker thread, not just the owning event loop —
    must not raise, and cancellation must still complete (round 2 review)."""
    sink = FakeSink()
    adapter = ElevenLabsAdapter(api_key="k", voice_id="v", stream_factory=_slow_stream, sink=sink)
    handle = await adapter.speak("hello")
    await asyncio.sleep(0)

    errors: list[BaseException] = []
    thread = threading.Thread(target=lambda: _safe_call(handle.stop, errors))
    thread.start()
    thread.join(timeout=2.0)

    assert errors == []
    with contextlib.suppress(asyncio.CancelledError):
        await handle._task  # type: ignore[attr-defined]  # noqa: SLF001
    assert handle.done()


def _safe_call(fn, errors: list[BaseException]) -> None:  # noqa: ANN001
    try:
        fn()
    except BaseException as exc:  # noqa: BLE001 - captured for the caller's assertion
        errors.append(exc)


async def test_elevenlabs_plain_callable_sink_without_close_still_works() -> None:
    """A plain callable sink (no close()) must not raise — existing callers/tests unchanged."""
    sink = FakeSink()  # no .close()
    adapter = ElevenLabsAdapter(api_key="k", voice_id="v", stream_factory=_fake_stream, sink=sink)
    await adapter.speak("hello")
    await asyncio.sleep(0)
    assert sink.received == [b"chunk1", b"chunk2"]  # ran to completion with no AttributeError


async def test_elevenlabs_default_sink_passes_output_device_env(
    monkeypatch: pytest.MonkeyPatch, fake_sounddevice: type[FakeRawOutputStream]
) -> None:
    """EDITH_OUTPUT_DEVICE reaches the real RawOutputStream constructor."""
    monkeypatch.setenv("EDITH_OUTPUT_DEVICE", "3")
    adapter = ElevenLabsAdapter(api_key="k", voice_id="v", stream_factory=_fake_stream)
    await adapter.speak("hello")
    await asyncio.sleep(0)
    assert fake_sounddevice.instances[-1].kwargs["device"] == 3
    assert fake_sounddevice.instances[-1].stopped  # closed exactly once via _RawOutputSink


async def test_elevenlabs_default_sink_device_absent_when_env_unset(
    monkeypatch: pytest.MonkeyPatch, fake_sounddevice: type[FakeRawOutputStream]
) -> None:
    """No env override -> device=None, the sounddevice default."""
    monkeypatch.delenv("EDITH_OUTPUT_DEVICE", raising=False)
    adapter = ElevenLabsAdapter(api_key="k", voice_id="v", stream_factory=_fake_stream)
    await adapter.speak("hello")
    await asyncio.sleep(0)
    assert fake_sounddevice.instances[-1].kwargs["device"] is None


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
    """stop() calls terminate() on the subprocess (blocking reader, no EOF).

    One tick after stop(): stop() schedules terminate()+cancel() via
    call_soon_threadsafe (round 2 review) rather than calling them synchronously.
    """
    sink = FakeSink()
    fake_proc = FakePiperProcess()  # Blocking reader — task never finishes on its own

    async def _runner(args: list[str]) -> FakePiperProcess:
        return fake_proc

    adapter = PiperAdapter(runner=_runner, sink=sink)
    handle = await adapter.speak("hello")
    handle.stop()
    await asyncio.sleep(0)
    assert fake_proc.terminated


async def test_piper_closes_closable_sink_on_normal_completion() -> None:
    """A sink exposing close() is closed exactly once when draining finishes normally."""
    sink = FakeClosableSink()
    fake_proc = FakePiperProcess(b"pcm_bytes")

    async def _runner(args: list[str]) -> FakePiperProcess:
        return fake_proc

    adapter = PiperAdapter(runner=_runner, sink=sink)
    await adapter.speak("hello")
    await asyncio.sleep(0)
    assert sink.close_calls == 1


async def test_piper_closes_closable_sink_on_stop_cancellation() -> None:
    """stop() terminates the process and cancels the drain task; sink still closes once."""
    sink = FakeClosableSink()
    fake_proc = FakePiperProcess()  # blocking reader — task never finishes on its own

    async def _runner(args: list[str]) -> FakePiperProcess:
        return fake_proc

    adapter = PiperAdapter(runner=_runner, sink=sink)
    handle = await adapter.speak("hello")
    await asyncio.sleep(0)  # let the drain task actually start (reach its first await)
    handle.stop()
    with contextlib.suppress(asyncio.CancelledError):
        await handle._task  # type: ignore[attr-defined]  # noqa: SLF001 — await cancellation
    assert sink.close_calls == 1


async def test_piper_default_sink_aborts_not_stops_on_cancellation(
    fake_sounddevice: type[FakeRawOutputStream],
) -> None:
    """Cancellation must discard queued audio via abort(), never drain via stop()."""
    fake_proc = FakePiperProcess()  # blocking reader — task never finishes on its own

    async def _runner(args: list[str]) -> FakePiperProcess:
        return fake_proc

    adapter = PiperAdapter(runner=_runner)
    handle = await adapter.speak("hello")
    await asyncio.sleep(0)  # let the drain task actually start
    handle.stop()
    with contextlib.suppress(asyncio.CancelledError):
        await handle._task  # type: ignore[attr-defined]  # noqa: SLF001
    stream = fake_sounddevice.instances[-1]
    assert stream.abort_calls == 1
    assert stream.stop_calls == 0
    assert stream.close_calls == 1


async def test_piper_default_sink_stops_not_aborts_on_normal_completion(
    fake_sounddevice: type[FakeRawOutputStream],
) -> None:
    fake_proc = FakePiperProcess(b"pcm_bytes")

    async def _runner(args: list[str]) -> FakePiperProcess:
        return fake_proc

    adapter = PiperAdapter(runner=_runner)
    await adapter.speak("hello")
    await asyncio.sleep(0)
    stream = fake_sounddevice.instances[-1]
    assert stream.stop_calls == 1
    assert stream.abort_calls == 0
    assert stream.close_calls == 1


async def test_piper_handle_stop_from_another_thread_is_thread_safe() -> None:
    """stop() can run from the mic worker thread, not just the owning event loop —
    must not raise, and terminate()+cancel() must still complete (round 2 review)."""
    fake_proc = FakePiperProcess()  # blocking reader

    async def _runner(args: list[str]) -> FakePiperProcess:
        return fake_proc

    adapter = PiperAdapter(runner=_runner, sink=FakeSink())
    handle = await adapter.speak("hello")
    await asyncio.sleep(0)

    errors: list[BaseException] = []
    thread = threading.Thread(target=lambda: _safe_call(handle.stop, errors))
    thread.start()
    thread.join(timeout=2.0)

    assert errors == []
    with contextlib.suppress(asyncio.CancelledError):
        await handle._task  # type: ignore[attr-defined]  # noqa: SLF001
    assert fake_proc.terminated
    assert handle.done()


async def test_piper_default_sink_passes_output_device_env(
    monkeypatch: pytest.MonkeyPatch, fake_sounddevice: type[FakeRawOutputStream]
) -> None:
    """EDITH_OUTPUT_DEVICE reaches the real RawOutputStream constructor."""
    monkeypatch.setenv("EDITH_OUTPUT_DEVICE", "headset")
    fake_proc = FakePiperProcess(b"pcm_bytes")

    async def _runner(args: list[str]) -> FakePiperProcess:
        return fake_proc

    adapter = PiperAdapter(runner=_runner)
    await adapter.speak("hello")
    await asyncio.sleep(0)
    assert fake_sounddevice.instances[-1].kwargs["device"] == "headset"


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
# handle.stop() tolerates a closed event loop (round 3 review): call_soon_threadsafe
# raises RuntimeError on an already-closed loop, which stop() must not let escape
# into the mic thread during a degraded/late shutdown.
# ---------------------------------------------------------------------------


class _FakeCancellableTask:
    """Stand-in for the ``asyncio.Task`` a handle cancels — no real event loop needed."""

    def __init__(self) -> None:
        self.cancel_calls = 0

    def cancel(self) -> None:
        self.cancel_calls += 1

    def done(self) -> bool:
        return False


class _ClosedFakeLoop:
    """A loop that already reports itself closed — call_soon_threadsafe must not even
    be attempted (a real closed loop would raise RuntimeError from that call)."""

    def is_closed(self) -> bool:
        return True

    def call_soon_threadsafe(self, callback: object) -> None:
        raise AssertionError("must not be called once the loop reports itself closed")


class _RaisingFakeLoop:
    """A loop that reports itself open but still raises on call_soon_threadsafe —
    simulates the loop closing in the race window between the is_closed() check and
    the scheduling call itself."""

    def is_closed(self) -> bool:
        return False

    def call_soon_threadsafe(self, callback: object) -> None:
        raise RuntimeError("Event loop is closed")


def test_elevenlabs_handle_stop_tolerates_an_already_closed_loop() -> None:
    task = _FakeCancellableTask()
    handle = _ElevenLabsHandle(task, _ClosedFakeLoop())  # type: ignore[arg-type]
    handle.stop()  # must not raise
    assert task.cancel_calls == 0  # nothing left to cancel once the loop is gone


def test_elevenlabs_handle_stop_tolerates_call_soon_threadsafe_raising_runtime_error() -> None:
    task = _FakeCancellableTask()
    handle = _ElevenLabsHandle(task, _RaisingFakeLoop())  # type: ignore[arg-type]
    handle.stop()  # must not raise even though call_soon_threadsafe itself raised
    assert task.cancel_calls == 0


async def test_piper_handle_stop_tolerates_an_already_closed_loop() -> None:
    proc = FakePiperProcess()  # needs a running loop for its StreamReader
    task = _FakeCancellableTask()
    handle = _PiperHandle(proc, task, _ClosedFakeLoop())  # type: ignore[arg-type]
    handle.stop()  # must not raise
    assert not proc.terminated
    assert task.cancel_calls == 0


async def test_piper_handle_stop_tolerates_call_soon_threadsafe_raising_runtime_error() -> None:
    proc = FakePiperProcess()  # needs a running loop for its StreamReader
    task = _FakeCancellableTask()
    handle = _PiperHandle(proc, task, _RaisingFakeLoop())  # type: ignore[arg-type]
    handle.stop()  # must not raise
    assert not proc.terminated
    assert task.cancel_calls == 0


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


# ---------------------------------------------------------------------------
# resolve_device_override — EDITH_INPUT_DEVICE / EDITH_OUTPUT_DEVICE parsing
# ---------------------------------------------------------------------------


def test_resolve_device_override_none_is_none() -> None:
    assert resolve_device_override(None) is None


def test_resolve_device_override_blank_is_none() -> None:
    assert resolve_device_override("") is None
    assert resolve_device_override("   ") is None


def test_resolve_device_override_digit_string_is_int() -> None:
    assert resolve_device_override("2") == 2
    assert resolve_device_override(" 0 ") == 0


def test_resolve_device_override_signed_integer_string_is_int() -> None:
    """Ordinary signed integers (e.g. -1) parse via int(), not just unsigned digits."""
    assert resolve_device_override("-1") == -1
    assert resolve_device_override(" -2 ") == -2


def test_resolve_device_override_non_digit_string_is_query() -> None:
    assert resolve_device_override("ULT WEAR") == "ULT WEAR"


def test_resolve_device_override_unicode_numeric_oddity_is_query_not_raise() -> None:
    """str.isdigit() is True for some Unicode numerics int() cannot parse (e.g. a
    superscript digit) — those must fall through as a device-name query, never raise."""
    assert resolve_device_override("\u00b2") == "\u00b2"  # superscript two — isdigit() True


def test_resolve_output_device_unset_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EDITH_OUTPUT_DEVICE", raising=False)
    assert resolve_output_device() is None


def test_resolve_output_device_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EDITH_OUTPUT_DEVICE", "1")
    assert resolve_output_device() == 1
