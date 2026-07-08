"""Voice I/O subsystem — wake word, STT, and TTS (north-star §4.3, spec 03).

This package owns EDITH's audio round-trip: always-listening wake-word detection
(openWakeWord), local speech-to-text (faster-whisper), and pluggable TTS output
(ElevenLabs streaming primary; Piper local fallback).

Exports from this package:
- :class:`TTSHandle` — cancellable playback handle protocol (barge-in support)
- :class:`TTSAdapter` — abstract base for TTS engines

VoiceIO orchestrator and concrete adapters (ElevenLabs, Piper) are added by
sibling tasks (#2 ``edith/voice/adapters/``, #3 ``edith/voice/voice_io.py``).
"""

from edith.voice.io import VoiceIO
from edith.voice.tts import TTSAdapter, TTSHandle

__all__ = ["TTSAdapter", "TTSHandle", "VoiceIO"]
