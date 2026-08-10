"""macOS Voice Processing I/O backend — Apple's echo canceller, the one FaceTime and Siri use.

VPIO is an *I/O unit*: it owns the microphone and the speaker together and cancels internally,
which is why it needs no reference signal plumbed to it and why it cannot run offline. Enabling
it on the input node enables it on the output node too.

Everything here was established by spiking against the real hardware, because several of the
obvious ways to write it are wrong in ways that produce plausible-looking garbage rather than
errors:

* ``buf.audioBufferList()`` raises an NSException inside the realtime tap callback, which is
  **fatal** — it terminates the interpreter. Do not call it.
* Indexing ``floatChannelData()`` past ``[0]`` reads unallocated memory. The input format claims
  9 channels on this hardware; only index 0 is real. Asking for a **mono tap format** is accepted
  and lets CoreAudio do the channel mapping.
* ``fcd[0].as_buffer(n * 4)`` returns the wrong region — it yields a constant value on every
  callback. The ctypes cast in :func:`channel_array` is the incantation that actually reads
  samples.
* Touching ``mainMixerNode()`` creates a default **44.1 kHz** connection that collides with
  VPIO's 48 kHz and the engine then fails to start with ``-10875``
  (``kAudioUnitErr_FailedInitialization``). Connect the player straight to ``outputNode``.

Measured on the owner's MacBook: capture RMS *fell* from 0.00226 (silent room) to 0.00050 while a
loud 440 Hz tone played through the speaker — the echo does not leak. Note the capture dropping
BELOW the ambient floor: VPIO is also applying residual suppression and AGC, and suppression that
aggressive is exactly what can crush near-end speech during double-talk. Whether this is genuinely
full duplex is what the bench's double-talk metric exists to answer; it is not settled here.

Owner live-smoke only — the pure arithmetic lives in ``resample.py`` and is unit-tested.
"""

from __future__ import annotations

import ctypes
import threading
from collections.abc import Iterator
from typing import Any

import numpy as np
from numpy.typing import NDArray

from edith.voice.duplex import FRAME_SAMPLES, SAMPLE_RATE, DuplexUnavailable
from edith.voice.duplex.resample import (
    VPIO_SAMPLE_RATE,
    decimate_to_16k,
    float_to_int16,
    int16_to_float,
    reblock,
)

# ElevenLabs streams pcm_24000; Piper emits 22.05 kHz. The caller declares which it is sending.
DEFAULT_PCM_SAMPLE_RATE = 24000

# VPIO hands over 100 ms buffers. Matching the tap size to that avoids extra re-blocking inside
# CoreAudio, and the queue below absorbs the mismatch with our own 80 ms frames.
TAP_BUFFER_FRAMES = 4800


def channel_array(buf: Any, index: int = 0) -> NDArray[np.float32]:
    """Read one channel out of an AVAudioPCMBuffer as a numpy array.

    ``as_buffer(n * 4)`` on the channel pointer silently returns the wrong region (a constant,
    identical on every callback). Casting the pointer's address through ctypes is what actually
    reaches the samples. The caller must ``.copy()`` before the buffer is recycled.
    """
    channel = buf.floatChannelData()[index]
    address = ctypes.addressof(ctypes.c_float.from_buffer(channel.as_buffer(4)))
    return np.ctypeslib.as_array(
        ctypes.cast(address, ctypes.POINTER(ctypes.c_float)), shape=(int(buf.frameLength()),)
    )


class VpioDuplex:
    """Full-duplex audio with macOS Voice Processing I/O doing the cancellation.

    Satisfies the ``DuplexAudio`` protocol: ``frames()`` yields cancelled 16 kHz mono int16
    frames of ``FRAME_SAMPLES``, ``play()`` accepts TTS PCM.
    """

    def __init__(
        self,
        frame_samples: int = FRAME_SAMPLES,
        pcm_sample_rate: int = DEFAULT_PCM_SAMPLE_RATE,
    ) -> None:
        try:
            import AVFoundation  # noqa: PLC0415 — optional macOS-only dependency
        except ImportError as exc:  # pragma: no cover - depends on the host
            raise DuplexUnavailable(
                "pyobjc-framework-AVFoundation is not installed; VPIO is unavailable"
            ) from exc

        self._av = AVFoundation
        self._frame_samples = frame_samples
        self._pcm_sample_rate = pcm_sample_rate
        self._closed = False

        # The realtime tap thread appends; frames() drains. Guarded because the two run on
        # different threads and a torn read here would corrupt a frame.
        self._lock = threading.Lock()
        self._pending: list[NDArray[np.int16]] = []
        self._tail: NDArray[np.int16] = np.zeros(0, dtype=np.int16)

        self._engine = AVFoundation.AVAudioEngine.alloc().init()
        self._input = self._engine.inputNode()
        enabled, error = self._input.setVoiceProcessingEnabled_error_(True, None)
        if not enabled:
            raise DuplexUnavailable(f"could not enable Voice Processing I/O: {error}")

        audio_format = AVFoundation.AVAudioFormat.alloc()
        self._format = audio_format.initStandardFormatWithSampleRate_channels_(
            float(VPIO_SAMPLE_RATE), 1
        )
        self._player = AVFoundation.AVAudioPlayerNode.alloc().init()
        self._engine.attachNode_(self._player)
        # Straight to outputNode. mainMixerNode would impose 44.1 kHz and the engine would
        # refuse to start with -10875.
        self._engine.connect_to_format_(self._player, self._engine.outputNode(), self._format)

        self._input.installTapOnBus_bufferSize_format_block_(
            0, TAP_BUFFER_FRAMES, self._format, self._on_tap
        )

        started, start_error = self._engine.startAndReturnError_(None)
        if not started:
            raise DuplexUnavailable(f"AVAudioEngine failed to start: {start_error}")
        self._player.play()

    def _on_tap(self, buf: Any, when: Any) -> None:
        """Realtime callback. Must never raise — an exception here terminates the process."""
        try:
            length = int(buf.frameLength())
            if length == 0:
                return
            captured = channel_array(buf).copy().astype(np.float64)
            downsampled = float_to_int16(decimate_to_16k(captured))
            with self._lock:
                combined = np.concatenate([self._tail, downsampled])
                frames, self._tail = reblock(combined, self._frame_samples)
                self._pending.extend(frames)
        except Exception:  # noqa: BLE001 — a realtime callback may not propagate anything
            return

    def frames(self) -> Iterator[NDArray[np.int16]]:
        """Yield cancelled 16 kHz mono int16 frames as the tap produces them."""
        while True:
            if self._closed:
                raise DuplexUnavailable("duplex is closed")
            with self._lock:
                ready = self._pending
                self._pending = []
            if not ready:
                continue
            yield from ready

    def play(self, pcm: bytes) -> None:
        """Schedule TTS PCM for playback through the VPIO output.

        *pcm* is mono int16 at ``pcm_sample_rate``. It is resampled to VPIO's 48 kHz — linear
        interpolation is adequate upsampling, since it adds no new aliases going up in rate.
        """
        if self._closed:
            raise DuplexUnavailable("duplex is closed")
        samples = int16_to_float(np.frombuffer(pcm, dtype=np.int16))
        if samples.size == 0:
            return
        if self._pcm_sample_rate != VPIO_SAMPLE_RATE:
            count = int(samples.size * VPIO_SAMPLE_RATE / self._pcm_sample_rate)
            samples = np.interp(
                np.linspace(0.0, samples.size - 1, count), np.arange(samples.size), samples
            )
        buffer = self._av.AVAudioPCMBuffer.alloc().initWithPCMFormat_frameCapacity_(
            self._format, samples.size
        )
        buffer.setFrameLength_(samples.size)
        channel_array(buffer)[:] = samples.astype(np.float32)
        self._player.scheduleBuffer_completionHandler_(buffer, None)

    def close(self) -> None:
        """Stop the engine and release the devices. Idempotent."""
        if self._closed:
            return
        self._closed = True
        try:
            self._player.stop()
            self._engine.stop()
            self._input.removeTapOnBus_(0)
        except Exception:  # noqa: BLE001 — teardown must not mask the caller's own failure
            return


def sample_rate() -> int:
    """The rate ``frames()`` yields at — what live.py and openWakeWord expect."""
    return SAMPLE_RATE
