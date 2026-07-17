"""Energy-based utterance endpointing — pure logic, no audio dependencies.

Feed one frame's RMS energy at a time via ``feed()``. Returns ``True`` when
the utterance should be considered complete (either sufficient trailing silence
after speech has started, or the hard-max wall-clock limit is reached).

**Threshold default (500.0) is a STARTING POINT that needs live calibration.**
With 16-bit PCM at 16 kHz, a quiet room typically reads RMS 50–300; conversational
speech typically reads 800–4 000+.  500.0 sits comfortably above typical ambient
noise on the owner's MacBook while reliably catching the onset of speech.

The right way to calibrate: run ``EDITH_VOICE_DEBUG=1`` and watch the
``mic_rms=`` heartbeat printed every ~1 s by ``edith/voice/live.py``.  Note the
quiet-room baseline and the level while talking, then set the threshold anywhere
between those two numbers.  Headless tests prove the *logic* (state machine
transitions), not the numeric calibration.
"""

from __future__ import annotations


class Endpointer:
    """Silence-based utterance endpointer fed one frame at a time.

    Parameters
    ----------
    silence_ms:
        How many consecutive milliseconds of sub-threshold RMS signal counts as
        "trailing silence" — the utterance ends when this is reached *after*
        speech has been detected.
    hard_max_ms:
        Absolute wall-clock ceiling in milliseconds.  The utterance ends when
        this is reached regardless of whether speech was ever detected.
    threshold:
        RMS value (int16 scale, 0–32 768) above which a frame is considered
        speech.  See module docstring for calibration guidance.
    frame_ms:
        Duration of each frame in milliseconds.  Must match the frame size used
        by the capture loop (1280 samples ÷ 16 000 Hz = 80 ms).
    """

    def __init__(
        self,
        silence_ms: float = 800.0,
        hard_max_ms: float = 15000.0,
        threshold: float = 500.0,
        frame_ms: float = 80.0,
    ) -> None:
        self._silence_ms = silence_ms
        self._hard_max_ms = hard_max_ms
        self._threshold = threshold
        self._frame_ms = frame_ms

        self._elapsed_ms: float = 0.0
        self._started: bool = False
        self._trailing_silence_ms: float = 0.0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def feed(self, rms: float) -> bool:
        """Feed one frame's RMS energy; return True when the utterance should end.

        Endpointing rules (in priority order):
        1. Hard-max: total elapsed >= hard_max_ms → end, regardless of speech.
        2. Trailing silence: speech was detected AND the trailing silence run
           has reached silence_ms → end.
        3. Otherwise: keep capturing.

        Leading silence (sub-threshold frames before any speech is seen) does
        NOT contribute to the trailing-silence counter, so the owner won't be
        cut off before they start talking.
        """
        self._elapsed_ms += self._frame_ms

        is_speech = rms >= self._threshold

        if is_speech:
            self._started = True
            self._trailing_silence_ms = 0.0
        elif self._started:
            # Only accumulate trailing silence once speech has begun.
            self._trailing_silence_ms += self._frame_ms

        # Rule 1 — hard max (fires even if speech never started).
        if self._elapsed_ms >= self._hard_max_ms:
            return True

        # Rule 2 — trailing silence after speech.
        return self._started and self._trailing_silence_ms >= self._silence_ms

    def reset(self) -> None:
        """Clear all state for the next utterance."""
        self._elapsed_ms = 0.0
        self._started = False
        self._trailing_silence_ms = 0.0

    # ------------------------------------------------------------------
    # Read-only helpers
    # ------------------------------------------------------------------

    @property
    def elapsed_ms(self) -> float:
        """Total milliseconds elapsed since the last reset (or construction)."""
        return self._elapsed_ms

    @property
    def started(self) -> bool:
        """True once at least one frame with rms >= threshold has been seen."""
        return self._started
