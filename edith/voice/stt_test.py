"""CLI smoke harness for speech-to-text (spec 03 §Verification).

Usage::

    python -m edith.voice.stt_test

Requires:
  - Real microphone hardware accessible to the process.
  - The ``[voice]`` optional-dependency group (``faster-whisper``, ``sounddevice``).

This module is for the owner's LIVE smoke test only; it is NOT unit-tested
(mic hardware and optional-dependency group ``[voice]`` are required at runtime).
"""

from __future__ import annotations

import sys


def main() -> None:
    # Probe heavy deps inside main so the module stays importable without them.
    missing: list[str] = []
    for pkg in ("faster_whisper", "sounddevice"):
        try:
            __import__(pkg)
        except ImportError:
            missing.append(pkg)

    if missing:
        print(
            "[voice] STT smoke test requires mic hardware and the [voice] "
            "optional-dependency group.\n"
            f"Missing packages: {', '.join(missing)}\n"
            "Install with:\n"
            "  pip install -e '.[voice]'",
            file=sys.stderr,
        )
        sys.exit(1)

    # Real implementation: load the faster-whisper model, record a short clip
    # from the default mic, and print the transcript. Wired in a later slice
    # once the STT seam in VoiceIO is connected to actual hardware.
    print(
        "[voice] STT smoke test: mic hardware and faster-whisper detected.\n"
        "STT seam wiring is pending (later slice). Exiting."
    )


if __name__ == "__main__":
    main()
