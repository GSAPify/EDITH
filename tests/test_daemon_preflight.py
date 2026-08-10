"""Capability preflight (`python -m edith.daemon --preflight`).

macOS TCC cannot be granted by a script, so the value here is entirely in *detection*:
provoking each prompt deliberately and reporting what is still missing. The probes take
injectable seams so the decision logic is tested headlessly; only the real hardware
calls are owner-smoke.
"""

from __future__ import annotations

from edith.daemon.preflight import (
    Check,
    check_apple_events,
    check_gateway,
    check_microphone,
    check_speech,
    check_wake_model,
    render,
)


def test_all_zero_samples_is_a_failure_not_a_quiet_room() -> None:
    """The whole reason this module exists.

    A denied microphone does not raise on macOS — CoreAudio returns a stream of digital
    silence. The wake word then never fires, nothing errors, and nothing is logged.
    Measured on the owner's machine: 32000 samples, every one exactly 0.
    """
    check = check_microphone(sample=lambda: [0] * 32000)
    assert not check.ok
    assert "digital" in check.detail
    assert "Privacy & Security" in check.fix


def test_real_audio_passes() -> None:
    check = check_microphone(sample=lambda: [0, 12, -340, 97])
    assert check.ok
    assert "340" in check.detail


def test_empty_stream_is_a_failure() -> None:
    assert not check_microphone(sample=list).ok


def test_mic_open_failure_is_reported_not_raised() -> None:
    def boom() -> list[int]:
        raise OSError("no default input device")

    check = check_microphone(sample=boom)
    assert not check.ok
    assert "no default input device" in check.detail


def test_apple_events_failure_carries_the_automation_fix() -> None:
    check = check_apple_events(runner=lambda _args: 1)
    assert not check.ok
    assert "Automation" in check.fix


def test_apple_events_success() -> None:
    assert check_apple_events(runner=lambda _args: 0).ok


def test_bundled_wake_model_is_flagged(monkeypatch) -> None:
    """hey_jarvis resolving means she is listening for the wrong phrase."""
    monkeypatch.delenv("EDITH_WAKE_MODEL", raising=False)
    check = check_wake_model()
    assert not check.ok
    assert "hey_edith" in check.fix


def test_missing_wake_model_file_is_flagged(monkeypatch) -> None:
    monkeypatch.setenv("EDITH_WAKE_MODEL", "/nope/hey_edith.onnx")
    check = check_wake_model(exists=lambda _p: False)
    assert not check.ok
    assert "does not exist" in check.detail


def test_present_wake_model_passes(monkeypatch) -> None:
    monkeypatch.setenv("EDITH_WAKE_MODEL", "/models/hey_edith.onnx")
    assert check_wake_model(exists=lambda _p: True).ok


def test_speech_prefers_elevenlabs_then_piper() -> None:
    assert check_speech({"ELEVENLABS_API_KEY": "k", "ELEVENLABS_VOICE_ID": "v"}).ok
    assert check_speech({"PIPER_MODEL": "/v.onnx"}).ok
    missing = check_speech({})
    assert not missing.ok
    assert "PIPER_MODEL" in missing.fix  # the piper -m trap, spelled out


def test_gateway_never_echoes_the_key() -> None:
    check = check_gateway(
        {"BIFROST_BASE_URL": "https://gw.example", "BIFROST_API_KEY": "sk-secret"}
    )
    assert check.ok
    assert "sk-secret" not in check.detail + check.fix + check.name


def test_render_lists_fixes_only_for_failures() -> None:
    report = render(
        [Check("A", True, "fine", "unused fix"), Check("B", False, "broken", "do the thing")]
    )
    assert "[ok  ] A: fine" in report
    assert "[FAIL] B: broken" in report
    assert "do the thing" in report
    assert "unused fix" not in report
    assert "cannot be granted from a script" in report


def test_render_all_passing() -> None:
    assert "All 1 checks passed" in render([Check("A", True, "fine")])


def test_unanswered_consent_dialog_is_a_failed_check_not_a_crash() -> None:
    """The probe exists to RAISE a consent dialog, so a timeout is the expected case.

    An unanswered dialog blocks osascript until the timeout. Letting TimeoutExpired
    propagate would take down the whole run and lose the checks that already passed —
    on exactly the machine this exists to diagnose.
    """
    import subprocess

    def hangs(_args: list[str]) -> int:
        raise subprocess.TimeoutExpired(cmd="osascript", timeout=20)

    check = check_apple_events(runner=hangs)
    assert not check.ok
    assert "consent dialog" in check.detail
    assert "Automation" in check.fix


def test_osascript_os_error_is_reported_not_raised() -> None:
    def boom(_args: list[str]) -> int:
        raise OSError("exec format error")

    check = check_apple_events(runner=boom)
    assert not check.ok
    assert "exec format error" in check.detail
