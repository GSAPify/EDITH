"""TTS adapters: ElevenLabs (primary) and Piper (local fallback) (spec 03 §Tech choices).

Heavy optional dependencies (``elevenlabs``, ``sounddevice``) are imported INSIDE
methods only — never at module top — so the core test suite runs without the
``voice`` optional-dependency group installed.

Concrete adapters satisfy :class:`edith.voice.tts.TTSAdapter`; callers obtain
one via :func:`select_adapter` or by constructing directly with injected deps.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Any, Protocol

from edith.voice.tts import TTSAdapter, TTSHandle

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Injectable-collaborator type aliases (seams for testing)
# ---------------------------------------------------------------------------

#: (api_key, voice_id, text) → async generator of raw PCM bytes chunks.
ElevenLabsStreamFactory = Callable[[str, str, str], AsyncGenerator[bytes, None]]

#: Consumes raw PCM bytes chunks for playback (e.g. sounddevice write).
AudioSink = Callable[[bytes], None]


class _PiperProcess(Protocol):
    """Structural interface the Piper subprocess must satisfy."""

    stdout: asyncio.StreamReader | None

    def terminate(self) -> None:
        ...


#: (arg list) → awaitable process handle.
PiperRunner = Callable[[list[str]], Awaitable[_PiperProcess]]


# ---------------------------------------------------------------------------
# Output device override + closable sink (bounded CoreAudio/PortAudio
# resilience fix — see docs/ENGINEERING-NOTES.md).
# ---------------------------------------------------------------------------


def resolve_device_override(raw: str | None) -> int | str | None:
    """Parse an ``EDITH_*_DEVICE`` env value into a ``sounddevice`` ``device=`` arg.

    Unset or blank keeps the sounddevice default (``None``); an ordinary signed
    integer string (``"2"``, ``"-1"``) is a device index; anything else is passed
    through as a name/query string, exactly as ``sounddevice`` itself accepts.

    Parses via ``int()`` directly rather than ``str.isdigit()`` + ``int()``:
    ``isdigit()`` returns True for Unicode numeric characters (e.g. superscripts,
    fullwidth digits) that ``int()`` itself cannot parse, which would raise
    unexpectedly on those inputs instead of treating them as a device-name query
    like any other non-integer string (round 2 review).
    """
    if raw is None:
        return None
    value = raw.strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return value


def resolve_output_device() -> int | str | None:
    """``EDITH_OUTPUT_DEVICE`` override for both real ``RawOutputStream`` adapters."""
    return resolve_device_override(os.environ.get("EDITH_OUTPUT_DEVICE"))


def _open_output_stream(sd_module: Any, *, samplerate: int, device: int | str | None) -> Any:
    """Start a ``RawOutputStream`` via the injected sounddevice module.

    A thin seam around the constructor + ``start()`` so the device override is
    unit-testable with a fake module — no hardware.

    If ``start()`` raises or is cancelled, the stream has already been
    constructed but no :class:`_RawOutputSink` is ever returned to reach any of
    the later teardown paths — closing it here, before re-raising, is the only
    chance to avoid leaking it (round 4 review). A teardown failure from that
    ``close()`` call is swallowed (logged, never raised) so it can never mask
    the original ``start()`` error/cancellation.
    """
    stream = sd_module.RawOutputStream(
        samplerate=samplerate, channels=1, dtype="int16", device=device
    )
    try:
        stream.start()
    except BaseException:
        with contextlib.suppress(Exception):
            stream.close()
        raise
    return stream


class _RawOutputSink:
    """Callable + closable/abortable wrapper around a started
    ``sounddevice.RawOutputStream``.

    Satisfies the plain :data:`AudioSink` callable contract via ``__call__`` (existing
    callers/tests that inject a bare function keep working unchanged) while adding two
    idempotent teardown seams, sharing one ``_closed`` guard so at most one of them ever
    runs the real stream teardown:

    - ``close()``  — normal-path teardown: ``stream.stop()`` waits for queued audio to
      finish draining before releasing the stream.
    - ``abort()``  — cancellation/error-path teardown (round 2 review): ``stream.abort()``
      discards queued audio immediately instead of draining it, so a barge-in actually
      cuts EDITH off rather than finishing the buffered tail first.

    Each ``speak()`` previously opened a new PortAudio output stream that was never
    closed — the adapters now call exactly one of these once per utterance on every
    exit path (normal completion, error, or cancellation). ``stream.close()`` is
    always attempted even if ``stream.stop()``/``stream.abort()`` itself raises
    (round 4 review) — otherwise a device error from the drain/discard call left the
    stream itself never released. The ``_closed`` guard is set BEFORE either call, so
    a raising first attempt still counts as "closed" and a retry stays a no-op.
    """

    def __init__(self, stream: Any) -> None:
        self._stream = stream
        self._closed = False

    def __call__(self, chunk: bytes) -> None:
        self._stream.write(chunk)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._stream.stop()
        finally:
            self._stream.close()

    def abort(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._stream.abort()
        finally:
            self._stream.close()


def _close_sink(sink: AudioSink) -> None:
    """Close *sink* if it exposes ``close()``; a plain callable sink is left alone."""
    close = getattr(sink, "close", None)
    if callable(close):
        close()


def _abort_sink(sink: AudioSink) -> None:
    """Abort *sink* if it exposes ``abort()``; fall back to ``close()`` for a sink that
    only has that (e.g. a test double), and leave a plain callable sink alone."""
    abort = getattr(sink, "abort", None)
    if callable(abort):
        abort()
        return
    _close_sink(sink)


def _schedule_threadsafe(loop: asyncio.AbstractEventLoop, callback: Callable[[], object]) -> None:
    """Schedule *callback* onto *loop* from any thread, tolerating a loop that has
    already closed during a degraded/late shutdown (round 3 review).

    ``handle.stop()`` can be called from the live mic loop's worker thread after the
    owning event loop has already been torn down (e.g. a stop request landing during
    the last gasp of shutdown), and ``loop.call_soon_threadsafe`` raises
    ``RuntimeError`` in that case. There is nothing left to cancel once the loop is
    gone, so this becomes a no-op rather than raising out of the mic thread. Both
    ``loop.is_closed()`` (the common case) and a ``RuntimeError`` from the call itself
    (the loop closing in the race window between the check and the call) are handled.
    """
    if loop.is_closed():
        return
    try:
        loop.call_soon_threadsafe(callback)
    except RuntimeError:
        _log.debug("voice: call_soon_threadsafe raised on a closing loop; ignoring")


def _teardown_sink(sink: AudioSink, *, aborting: bool) -> None:
    """Tear down *sink* exactly once, choosing ``abort()`` (aborting) or ``close()``
    (normal completion) — see :class:`_RawOutputSink`.

    Swallows (logs, never raises) any exception the teardown call itself raises: this
    always runs from a caller's ``finally``, alongside a cancellation or a stream error
    already being propagated, and a teardown failure must never replace/mask that
    original error (round 2 review).
    """
    try:
        if aborting:
            _abort_sink(sink)
        else:
            _close_sink(sink)
    except Exception:  # noqa: BLE001 - deliberately never propagated, see docstring
        _log.debug("voice: sink teardown raised; ignoring (see _teardown_sink)", exc_info=True)


# ---------------------------------------------------------------------------
# ElevenLabs adapter
# ---------------------------------------------------------------------------


class _ElevenLabsHandle:
    """TTSHandle that cancels the in-flight background streaming task on stop()."""

    def __init__(self, task: asyncio.Task[None], loop: asyncio.AbstractEventLoop) -> None:
        self._task = task
        self._loop = loop

    def stop(self) -> None:
        """Cancel the ElevenLabs streaming task immediately.

        ``stop()`` can be called from the live mic loop's worker thread (barge-in
        landing while the mic thread is between frames), not just from the owning
        event loop — ``asyncio.Task.cancel()`` is not itself thread-safe, so scheduling
        it via ``call_soon_threadsafe`` (round 2 review) is required rather than
        optional here. Routed through ``_schedule_threadsafe`` (round 3 review) so a
        loop that has already closed during degraded shutdown does not raise out of
        the mic thread.
        """
        _schedule_threadsafe(self._loop, self._task.cancel)

    def done(self) -> bool:
        """True once the streaming task has finished or been cancelled."""
        return self._task.done()


class ElevenLabsAdapter(TTSAdapter):
    """Streams PCM audio from the ElevenLabs API to an audio sink.

    *stream_factory* and *sink* are dependency-injected for testing.  When
    omitted the real ``elevenlabs`` and ``sounddevice`` libraries are imported
    lazily inside :meth:`speak` — never at module import time.
    """

    def __init__(
        self,
        api_key: str,
        voice_id: str,
        stream_factory: ElevenLabsStreamFactory | None = None,
        sink: AudioSink | None = None,
    ) -> None:
        self._api_key = api_key
        self._voice_id = voice_id
        self._stream_factory = stream_factory
        self._sink = sink

    def name(self) -> str:
        return "elevenlabs"

    async def speak(self, text: str) -> TTSHandle:
        """Start ElevenLabs streaming in a background task; return a cancellable handle."""
        factory = (
            self._stream_factory if self._stream_factory is not None else self._default_factory()
        )
        sink = self._sink if self._sink is not None else self._default_sink()
        loop = asyncio.get_running_loop()
        task: asyncio.Task[None] = asyncio.create_task(self._run_stream(text, factory, sink))
        return _ElevenLabsHandle(task, loop)

    def _default_factory(self) -> ElevenLabsStreamFactory:
        """Build a streaming factory over the ElevenLabs **v2** async client.

        Imports ``elevenlabs`` lazily (only when no factory is injected), so the
        module still imports without the ``[voice]`` extra. Requests raw
        ``pcm_24000`` so chunks feed a 24 kHz sounddevice stream directly.

        NOT headless-verified — needs a real API key + network (owner live-smoke).
        PCM output formats require a paid ElevenLabs tier; on a free key, switch
        ``output_format`` to an mp3 variant and decode before the sink.
        """

        async def _stream(api_key: str, voice_id: str, text: str) -> AsyncGenerator[bytes, None]:
            from elevenlabs.client import AsyncElevenLabs

            client = AsyncElevenLabs(api_key=api_key)
            async for chunk in client.text_to_speech.stream(
                voice_id=voice_id,
                text=text,
                model_id="eleven_turbo_v2_5",
                output_format="pcm_24000",
            ):
                yield chunk

        return _stream

    def _default_sink(self) -> AudioSink:
        """Build a sounddevice sink at 24 kHz to match the ``pcm_24000`` stream.

        Imports ``sounddevice`` lazily. NOT headless-verified (needs a speaker).
        ``EDITH_OUTPUT_DEVICE`` overrides the default output device.
        """
        import sounddevice as sd

        device = resolve_output_device()
        if os.environ.get("EDITH_VOICE_DEBUG") == "1" and device is not None:
            _log.info("voice: output device override = %r", device)
        stream = _open_output_stream(sd, samplerate=24000, device=device)
        return _RawOutputSink(stream)

    async def _run_stream(
        self, text: str, factory: ElevenLabsStreamFactory, sink: AudioSink
    ) -> None:
        aborting = False
        try:
            async for chunk in factory(self._api_key, self._voice_id, text):
                sink(chunk)
        except BaseException:
            # Cancellation (barge-in via handle.stop()) or a genuine stream error:
            # discard queued audio via abort() rather than draining it — see
            # _RawOutputSink. The original error/cancellation always wins over
            # anything _teardown_sink itself raises (round 2 review).
            aborting = True
            raise
        finally:
            _teardown_sink(sink, aborting=aborting)


# ---------------------------------------------------------------------------
# Piper adapter
# ---------------------------------------------------------------------------


class _PiperHandle:
    """TTSHandle that terminates the Piper subprocess and cancels the drain task."""

    def __init__(
        self, proc: _PiperProcess, task: asyncio.Task[None], loop: asyncio.AbstractEventLoop
    ) -> None:
        self._proc = proc
        self._task = task
        self._loop = loop

    def stop(self) -> None:
        """Terminate the Piper subprocess and cancel the background drain task.

        ``stop()`` can be called from the live mic loop's worker thread — scheduled
        together onto the owning event loop via ``call_soon_threadsafe`` (round 2
        review), since ``asyncio.Task.cancel()`` is not itself thread-safe. Routed
        through ``_schedule_threadsafe`` (round 3 review) so a loop that has already
        closed during degraded shutdown does not raise out of the mic thread.
        """
        _schedule_threadsafe(self._loop, self._stop_on_loop)

    def _stop_on_loop(self) -> None:
        self._proc.terminate()
        self._task.cancel()

    def done(self) -> bool:
        """True once the drain task has finished or been cancelled."""
        return self._task.done()


class PiperAdapter(TTSAdapter):
    """Runs the ``piper`` local TTS binary as a subprocess; streams PCM to a sink.

    *runner* and *sink* are dependency-injected for testing.  When omitted the
    real ``piper`` binary is invoked via :func:`asyncio.create_subprocess_exec`
    and ``sounddevice`` is imported lazily.
    """

    def __init__(
        self,
        model_path: str = "",
        runner: PiperRunner | None = None,
        sink: AudioSink | None = None,
    ) -> None:
        self._model_path = model_path
        self._runner = runner
        self._sink = sink

    def name(self) -> str:
        return "piper"

    async def speak(self, text: str) -> TTSHandle:
        """Spawn the Piper subprocess in a background task; return a cancellable handle.

        The output sink is opened only AFTER ``runner(args)`` succeeds (round 4
        review): opening it first meant a runner failure or cancellation left a
        stream open with no ``_drain`` task ever created to close it in its
        ``finally``. If sink/task setup fails once the process HAS started, the
        process is terminated and the already-open sink torn down — via
        ``_teardown_sink``, so a teardown failure there can never mask the
        original setup failure.
        """
        runner: PiperRunner = self._runner if self._runner is not None else _default_piper_runner

        args = ["piper", "--output-raw"]
        if self._model_path:
            args.extend(["--model", self._model_path])
        args.extend(["--text", text])

        proc = await runner(args)
        sink: AudioSink | None = None
        try:
            sink = self._sink if self._sink is not None else self._default_sink()
            loop = asyncio.get_running_loop()
            task: asyncio.Task[None] = asyncio.create_task(self._drain(proc, sink))
            return _PiperHandle(proc, task, loop)
        except BaseException:
            proc.terminate()
            if sink is not None:
                _teardown_sink(sink, aborting=True)
            raise

    def _default_sink(self) -> AudioSink:
        """Build an audio sink that imports ``sounddevice`` lazily.

        ``EDITH_OUTPUT_DEVICE`` overrides the default output device.
        """
        import sounddevice as sd  # pyright: ignore[reportMissingImports]

        device = resolve_output_device()
        if os.environ.get("EDITH_VOICE_DEBUG") == "1" and device is not None:
            _log.info("voice: output device override = %r", device)
        stream = _open_output_stream(sd, samplerate=22050, device=device)
        return _RawOutputSink(stream)

    async def _drain(self, proc: _PiperProcess, sink: AudioSink) -> None:
        aborting = False
        try:
            # No early `return` for the stdout-is-None case: it must still fall
            # through to the normal (non-aborting) `finally` teardown below —
            # nesting under `if` rather than returning keeps that path identical
            # to every other no-exception exit.
            if proc.stdout is not None:
                while True:
                    chunk = await proc.stdout.read(4096)
                    if not chunk:
                        break
                    sink(chunk)
        except BaseException:
            # Cancellation (barge-in via handle.stop()) or a genuine read error:
            # discard queued audio via abort() rather than draining it. The original
            # error/cancellation always wins over anything _teardown_sink itself
            # raises (round 2 review).
            aborting = True
            raise
        finally:
            _teardown_sink(sink, aborting=aborting)


async def _default_piper_runner(args: list[str]) -> _PiperProcess:
    """Default Piper runner — shells to the real ``piper`` binary via arg list."""
    return await asyncio.create_subprocess_exec(
        args[0],
        *args[1:],
        stdout=asyncio.subprocess.PIPE,
    )


# ---------------------------------------------------------------------------
# Factory / engine selector
# ---------------------------------------------------------------------------


def select_adapter(
    engine: str,
    *,
    api_key: str = "",
    voice_id: str = "",
    model_path: str = "",
    stream_factory: ElevenLabsStreamFactory | None = None,
    sink: AudioSink | None = None,
    runner: PiperRunner | None = None,
) -> TTSAdapter:
    """Return a :class:`TTSAdapter` for *engine*.

    :raises ValueError: if *engine* is not ``"elevenlabs"`` or ``"piper"``.
    """
    if engine == "elevenlabs":
        return ElevenLabsAdapter(
            api_key=api_key,
            voice_id=voice_id,
            stream_factory=stream_factory,
            sink=sink,
        )
    if engine == "piper":
        return PiperAdapter(
            model_path=model_path,
            runner=runner,
            sink=sink,
        )
    raise ValueError(f"Unknown TTS engine: {engine!r}; expected 'elevenlabs' or 'piper'")
