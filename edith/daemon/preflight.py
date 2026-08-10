"""Capability preflight — provoke every macOS permission prompt, then report.

macOS permissions (TCC) **cannot be granted by a script.** There is no supported API
that says "grant this app the microphone"; the grant only happens when the user clicks
a system dialog, and that dialog only appears when the app actually *asks* for the
capability. Anything that claims to grant TCC non-interactively either silently fails
or needs SIP disabled.

So this does the one thing that genuinely helps: it touches each capability once, on
purpose and in isolation, so macOS raises its own prompt while the owner is sitting
there expecting it — instead of mid-conversation with the daemon, where a missed
prompt looks like a broken feature. Then it prints exactly what is still missing.

The failure mode this exists to catch is the silent one. **A denied microphone does
not raise** — CoreAudio hands back a stream of digital silence, so the wake word never
fires, nothing errors, and nothing is logged. That is indistinguishable from a quiet
room unless you check whether every sample is exactly zero, which is what
``check_microphone`` does.

Every probe takes an injectable seam, so the decision logic is unit-tested headlessly
and only the hardware calls are owner-smoke.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

_SAMPLE_RATE = 16000
_FRAME = 1280
_PROBE_FRAMES = 12  # ~1 second, enough to tell silence from a quiet room


@dataclass(frozen=True)
class Check:
    """One capability probe's verdict."""

    name: str
    ok: bool
    detail: str
    fix: str = ""


def check_microphone(sample: Callable[[], list[int]] | None = None) -> Check:
    """Open the mic and prove real audio arrives — not TCC's silent zero-fill.

    A denied mic yields a successful stream whose every sample is 0. Treating "opened
    without raising" as success is exactly how this hid before.
    """
    probe = sample if sample is not None else _default_mic_sample
    try:
        frames = probe()
    except Exception as exc:  # noqa: BLE001 — any audio-stack failure is a failed check
        return Check(
            "Microphone",
            False,
            f"could not open an input stream: {type(exc).__name__}: {exc}",
            "Check System Settings > Sound > Input that a device is selected.",
        )
    if not frames:
        return Check("Microphone", False, "input stream returned no samples", _MIC_FIX)
    peak = max(abs(s) for s in frames)
    if peak == 0:
        return Check(
            "Microphone",
            False,
            f"{len(frames)} samples, every one exactly 0 — TCC is returning digital "
            "silence, not a quiet room",
            _MIC_FIX,
        )
    return Check("Microphone", True, f"live (peak sample {peak})")


_MIC_FIX = (
    "System Settings > Privacy & Security > Microphone — enable your terminal app, "
    "then restart it. If no prompt ever appeared, the terminal was denied earlier; "
    "macOS does not ask twice."
)


def check_apple_events(runner: Callable[[list[str]], int] | None = None) -> Check:
    """Provoke the Automation (Apple Events) prompt that desktop control needs.

    ``DesktopControlSkill`` drives Spotify/Terminal via ``osascript``. The first such
    call raises a per-target consent dialog; until it is answered the action fails at
    the moment the owner asked for it.
    """
    run = runner if runner is not None else _default_osascript
    if shutil.which("osascript") is None:
        return Check("Apple Events", False, "osascript not found on PATH", "")
    code = run(["osascript", "-e", 'tell application "System Events" to get name'])
    if code == 0:
        return Check("Apple Events", True, "System Events reachable")
    return Check(
        "Apple Events",
        False,
        f"osascript exited {code}",
        "System Settings > Privacy & Security > Automation — allow your terminal to "
        "control System Events (and Spotify / Terminal for desktop control).",
    )


def check_wake_model(exists: Callable[[str], bool] | None = None) -> Check:
    """The trained wake model must resolve, or she listens for the wrong phrase."""
    from edith.voice.live import resolve_wake_model

    model = resolve_wake_model()
    if not model.endswith(".onnx"):
        return Check(
            "Wake model",
            False,
            f"{model!r} is a bundled openWakeWord name, not the trained hey_edith model",
            "Set EDITH_WAKE_MODEL to the path of hey_edith.onnx in .env.",
        )
    is_file = exists if exists is not None else os.path.isfile
    if not is_file(model):
        return Check("Wake model", False, f"{model} does not exist", "Retrain or restore it.")
    return Check("Wake model", True, model)


def check_speech(env: dict[str, str] | None = None) -> Check:
    """TTS credentials — without these she hears but cannot answer."""
    environ = env if env is not None else dict(os.environ)
    if environ.get("ELEVENLABS_API_KEY") and environ.get("ELEVENLABS_VOICE_ID"):
        return Check("Speech (ElevenLabs)", True, "key and voice id present")
    if environ.get("PIPER_MODEL"):
        return Check("Speech (Piper)", True, "PIPER_MODEL set")
    return Check(
        "Speech",
        False,
        "no ElevenLabs key/voice id and no PIPER_MODEL",
        "Set ELEVENLABS_API_KEY + ELEVENLABS_VOICE_ID in .env, and run with "
        "--engine elevenlabs. Piper needs PIPER_MODEL — it has a REQUIRED -m flag "
        "and is not on launchd's PATH.",
    )


def check_gateway(env: dict[str, str] | None = None) -> Check:
    """Model-gateway config. Never prints the key — only whether one is present."""
    environ = env if env is not None else dict(os.environ)
    if environ.get("BIFROST_BASE_URL") and environ.get("BIFROST_API_KEY"):
        return Check("Model gateway", True, environ["BIFROST_BASE_URL"])
    return Check(
        "Model gateway",
        False,
        "BIFROST_BASE_URL / BIFROST_API_KEY not both set",
        "source .env before starting the daemon.",
    )


def run_all() -> list[Check]:
    """Every probe, in the order the daemon needs them."""
    return [
        check_microphone(),
        check_wake_model(),
        check_speech(),
        check_gateway(),
        check_apple_events(),
    ]


def render(checks: list[Check]) -> str:
    """Human-readable report; failures carry their fix."""
    lines = ["", "EDITH preflight", "=" * 60]
    for check in checks:
        mark = "ok  " if check.ok else "FAIL"
        lines.append(f"[{mark}] {check.name}: {check.detail}")
        if not check.ok and check.fix:
            lines.append(f"        -> {check.fix}")
    failed = [c for c in checks if not c.ok]
    lines.append("=" * 60)
    if failed:
        lines.append(
            f"{len(failed)} of {len(checks)} checks failed. macOS permissions cannot be "
            "granted from a script — the prompts above (or System Settings) are the only way."
        )
    else:
        lines.append(f"All {len(checks)} checks passed — she should hear you and answer.")
    return "\n".join(lines) + "\n"


def _default_mic_sample() -> list[int]:
    """Read ~1 s of int16 PCM off the default input device."""
    import numpy as np
    import sounddevice as sd

    frames: list[int] = []
    with sd.RawInputStream(
        samplerate=_SAMPLE_RATE, channels=1, dtype="int16", blocksize=_FRAME
    ) as stream:
        for _ in range(_PROBE_FRAMES):
            data, _overflowed = stream.read(_FRAME)
            frames.extend(np.frombuffer(bytes(data), dtype=np.int16).tolist())
    return frames


def _default_osascript(args: list[str]) -> int:
    return subprocess.run(args, capture_output=True, timeout=20, check=False).returncode
