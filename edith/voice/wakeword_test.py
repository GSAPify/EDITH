"""CLI smoke harness for wake-word detection (spec 03 §Verification).

Usage::

    python -m edith.voice.wakeword_test

Requires:
  - Real microphone hardware accessible to the process.
  - The ``[voice]`` optional-dependency group (``openWakeWord``, ``sounddevice``).

This module is for the owner's LIVE smoke test only; it is NOT unit-tested
(mic hardware and optional-dependency group ``[voice]`` are required at runtime).
"""

from __future__ import annotations

import sys


def main() -> None:
    # Probe heavy deps inside main so the module stays importable without them.
    missing: list[str] = []
    for pkg in ("openwakeword", "sounddevice"):
        try:
            __import__(pkg)
        except ImportError:
            missing.append(pkg)

    if missing:
        print(
            "[voice] Wake-word smoke test requires mic hardware and the [voice] "
            "optional-dependency group.\n"
            f"Missing packages: {', '.join(missing)}\n"
            "Install with:\n"
            "  pip install -e '.[voice]'",
            file=sys.stderr,
        )
        sys.exit(1)

    # Real implementation: initialise the openWakeWord model, open the default
    # mic stream, and print detections to stdout. Wired in a later slice once
    # the wake-detector seam in VoiceIO is connected to actual hardware.
    print(
        "[voice] Wake-word smoke test: mic hardware detected.\n"
        "Wake-detector seam wiring is pending (later slice). Exiting."
    )


if __name__ == "__main__":
    main()
