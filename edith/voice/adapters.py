"""TTS adapters: ElevenLabs (primary) and Piper (local fallback) (spec 03 §Tech choices).

Heavy optional dependencies (``elevenlabs``, ``sounddevice``) are imported INSIDE
methods only — never at module top — so the core test suite runs without the
``voice`` optional-dependency group installed.

Concrete adapters satisfy :class:`edith.voice.tts.TTSAdapter`; callers obtain
one via :func:`select_adapter` or by constructing directly with injected deps.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Protocol

from edith.voice.tts import TTSAdapter, TTSHandle

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
# ElevenLabs adapter
# ---------------------------------------------------------------------------


class _ElevenLabsHandle:
    """TTSHandle that cancels the in-flight background streaming task on stop()."""

    def __init__(self, task: asyncio.Task[None]) -> None:
        self._task = task

    def stop(self) -> None:
        """Cancel the ElevenLabs streaming task immediately."""
        self._task.cancel()

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
        task: asyncio.Task[None] = asyncio.create_task(self._run_stream(text, factory, sink))
        return _ElevenLabsHandle(task)

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
        """
        import sounddevice as sd

        out = sd.RawOutputStream(samplerate=24000, channels=1, dtype="int16")
        out.start()

        def _write(chunk: bytes) -> None:
            out.write(chunk)

        return _write

    async def _run_stream(
        self, text: str, factory: ElevenLabsStreamFactory, sink: AudioSink
    ) -> None:
        async for chunk in factory(self._api_key, self._voice_id, text):
            sink(chunk)


# ---------------------------------------------------------------------------
# Piper adapter
# ---------------------------------------------------------------------------


class _PiperHandle:
    """TTSHandle that terminates the Piper subprocess and cancels the drain task."""

    def __init__(self, proc: _PiperProcess, task: asyncio.Task[None]) -> None:
        self._proc = proc
        self._task = task

    def stop(self) -> None:
        """Terminate the Piper subprocess and cancel the background drain task."""
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
        """Spawn the Piper subprocess in a background task; return a cancellable handle."""
        runner: PiperRunner = self._runner if self._runner is not None else _default_piper_runner
        sink = self._sink if self._sink is not None else self._default_sink()

        args = ["piper", "--output-raw"]
        if self._model_path:
            args.extend(["--model", self._model_path])
        args.extend(["--text", text])

        proc = await runner(args)
        task: asyncio.Task[None] = asyncio.create_task(self._drain(proc, sink))
        return _PiperHandle(proc, task)

    def _default_sink(self) -> AudioSink:
        """Build an audio sink that imports ``sounddevice`` lazily."""
        import sounddevice as sd  # pyright: ignore[reportMissingImports]

        out = sd.RawOutputStream(samplerate=22050, channels=1, dtype="int16")
        out.start()

        def _write(chunk: bytes) -> None:
            out.write(chunk)

        return _write

    async def _drain(self, proc: _PiperProcess, sink: AudioSink) -> None:
        if proc.stdout is None:
            return
        while True:
            chunk = await proc.stdout.read(4096)
            if not chunk:
                break
            sink(chunk)


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
