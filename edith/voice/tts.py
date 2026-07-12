"""TTS adapter interface (spec 03 §TTS adapter interface).

Defines the ``TTSHandle`` protocol (cancellable playback handle for barge-in) and
the ``TTSAdapter`` abstract base (pluggable TTS engine seam).  Concrete adapters
— ElevenLabs streaming and Piper local fallback — live in sibling modules added
by Task #2.  No heavy dependencies are imported here so the test suite runs with
zero audio or cloud libraries installed.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Protocol, runtime_checkable


@runtime_checkable
class TTSHandle(Protocol):
    """Cancellable playback handle returned by :meth:`TTSAdapter.speak` (spec 03 §Barge-in).

    VoiceIO holds this reference; on a new wake-word event it calls ``stop()`` to
    drain the audio buffer and abort the in-flight ElevenLabs stream or Piper
    subprocess before publishing ``voice.wake`` to the bus.
    """

    def stop(self) -> None:
        """Cancel active TTS playback immediately."""
        ...

    def done(self) -> bool:
        """True once playback has finished (or was stopped/cancelled).

        Drives ``VoiceIO.is_speaking`` for half-duplex mic gating: the live loop
        suppresses wake detection while a handle is still playing so EDITH never
        hears — and re-triggers on — her own TTS.
        """
        ...


class TTSAdapter(ABC):
    """Abstract base for pluggable TTS engines (spec 03 §TTS adapter interface).

    Concrete subclasses implement :meth:`speak` (returns a :class:`TTSHandle` for
    barge-in cancellation) and :meth:`name` for logging and engine selection.

    Heavy dependencies (``elevenlabs``, ``piper-tts``, ``sounddevice``) are guarded
    inside each concrete adapter — never imported at this module level — so the
    test suite runs without the ``voice`` optional-dependency group installed.
    """

    @abstractmethod
    async def speak(self, text: str) -> TTSHandle:
        """Start TTS playback for *text* and return a cancellable handle."""

    @abstractmethod
    def name(self) -> str:
        """Human-readable adapter name (e.g. ``"elevenlabs"``, ``"piper"``)."""
