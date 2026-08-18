"""TDD tests for the bounded CoreAudio/PortAudio input-stream resilience fix
(spec 10 review — daemon/Control API survived ``PaMacCore (AUHAL) err=-50``; the
voice loop itself had no containment/reopen path).

All tests use fakes only — no hardware, no real sleeps. ``_run_recoverable`` is a
generic seam: the real caller (``_blocking_listen``) supplies ``sd.RawInputStream``
and ``sd.PortAudioError``, but the retry/reopen/reset logic itself never touches
sounddevice, so it is exercised here with plain fakes and a made-up error type.
"""

from __future__ import annotations

import contextlib
import threading

import pytest

from edith.voice.live import (
    _open_input_stream,
    _run_recoverable,
    _sleep_wait,
    resolve_audio_max_retries,
    resolve_audio_retry_seconds,
    resolve_input_device,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakePortAudioError(Exception):
    """Stand-in for ``sounddevice.PortAudioError`` — no sounddevice import needed."""


@contextlib.contextmanager
def _ok_stream():
    yield object()


# ---------------------------------------------------------------------------
# _run_recoverable — reopen after error, reset called, eventually healthy
# ---------------------------------------------------------------------------


def test_recovery_reopens_after_error_calls_reset_and_runs_healthy_stream() -> None:
    calls: list[str] = []
    attempts = {"n": 0}

    def open_stream():
        calls.append("open")
        return _ok_stream()

    def listen(stream: object) -> None:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise _FakePortAudioError("PaMacCore (AUHAL) err=-50")
        calls.append("listen-ok")  # second attempt runs to completion (stop implied set)

    def reset() -> None:
        calls.append("reset")

    waits: list[float] = []

    def wait(seconds: float) -> bool:
        waits.append(seconds)
        return False  # not interrupted -> retry

    _run_recoverable(
        open_stream,
        listen,
        reset=reset,
        error_types=(_FakePortAudioError,),
        wait=wait,
        retry_seconds=1.0,
        stop=None,
    )

    assert calls == ["open", "reset", "open", "listen-ok"]
    assert waits == [1.0]  # exactly one bounded wait between the failed and healthy attempt


def test_recovery_handles_error_raised_while_opening_the_stream() -> None:
    """An error from open_stream() itself (not just listen()) is caught and recovered."""
    calls: list[str] = []
    attempts = {"n": 0}

    def open_stream():
        attempts["n"] += 1
        calls.append("open")
        if attempts["n"] == 1:
            raise _FakePortAudioError("device unavailable")
        return _ok_stream()

    def listen(stream: object) -> None:
        calls.append("listen-ok")

    def reset() -> None:
        calls.append("reset")

    def wait(seconds: float) -> bool:
        calls.append(f"wait:{seconds}")
        return False

    _run_recoverable(
        open_stream,
        listen,
        reset=reset,
        error_types=(_FakePortAudioError,),
        wait=wait,
        retry_seconds=0.5,
        stop=None,
    )

    assert calls == ["open", "reset", "wait:0.5", "open", "listen-ok"]


def test_recovery_does_not_retry_on_a_normal_return() -> None:
    """listen() returning normally (stop was set) must end the loop, not reopen."""
    open_calls = {"n": 0}

    def open_stream():
        open_calls["n"] += 1
        return _ok_stream()

    def listen(stream: object) -> None:
        return None  # normal completion

    _run_recoverable(
        open_stream,
        listen,
        reset=lambda: None,
        error_types=(_FakePortAudioError,),
        wait=lambda seconds: False,
        retry_seconds=1.0,
        stop=None,
    )

    assert open_calls["n"] == 1


# ---------------------------------------------------------------------------
# Stop event interrupts retry — no busy loop
# ---------------------------------------------------------------------------


def test_stop_interrupts_retry_wait_and_stops_retrying() -> None:
    """wait() returning True (stop requested) ends the loop immediately — exactly
    one open, one reset, one wait; no second reopen (no busy loop)."""
    calls: list[str] = []

    def open_stream():
        calls.append("open")
        return _ok_stream()

    def listen(stream: object) -> None:
        raise _FakePortAudioError("still broken")

    def reset() -> None:
        calls.append("reset")

    def wait(seconds: float) -> bool:
        calls.append(f"wait:{seconds}")
        return True  # stop was requested during the wait

    _run_recoverable(
        open_stream,
        listen,
        reset=reset,
        error_types=(_FakePortAudioError,),
        wait=wait,
        retry_seconds=2.5,
        stop=None,
    )

    assert calls == ["open", "reset", "wait:2.5"]  # one attempt only — no busy loop


def test_stop_event_already_set_never_opens_the_stream() -> None:
    """A stop event set BEFORE the loop starts means zero attempts at all."""
    stop = threading.Event()
    stop.set()
    open_calls = {"n": 0}

    def open_stream():
        open_calls["n"] += 1
        return _ok_stream()

    _run_recoverable(
        open_stream,
        lambda stream: None,
        reset=lambda: None,
        error_types=(_FakePortAudioError,),
        wait=stop.wait,
        retry_seconds=1.0,
        stop=stop,
    )

    assert open_calls["n"] == 0


def test_real_threading_event_wait_interrupts_promptly() -> None:
    """Integration check with a REAL threading.Event: setting it during the retry
    wait must interrupt promptly (threading.Event.wait returns True once set)."""
    stop = threading.Event()
    calls: list[str] = []

    def open_stream():
        calls.append("open")
        return _ok_stream()

    def listen(stream: object) -> None:
        if len(calls) == 1:
            stop.set()  # simulate the stop request landing while the stream is "up"
            raise _FakePortAudioError("boom")

    _run_recoverable(
        open_stream,
        listen,
        reset=lambda: None,
        error_types=(_FakePortAudioError,),
        wait=stop.wait,
        retry_seconds=5.0,  # would hang the test for 5s if wait() were not interrupted
        stop=stop,
    )

    assert calls == ["open"]  # never reopened — the (already-set) stop interrupted the wait


# ---------------------------------------------------------------------------
# _run_recoverable — consecutive-failure ceiling (round 2 review: an unbounded
# retry loop for a permanently missing/bad input device never exits, so the
# voice task's failure could never surface via _on_voice_task_done).
# ---------------------------------------------------------------------------


def test_recovery_reraises_after_max_retries_reached() -> None:
    """A permanently failing open re-raises exactly at the configured ceiling —
    no extra open/reset/wait beyond it."""
    calls: list[str] = []

    def open_stream():
        calls.append("open")
        raise _FakePortAudioError("device permanently gone")

    def wait(seconds: float) -> bool:
        calls.append(f"wait:{seconds}")
        return False

    with pytest.raises(_FakePortAudioError):
        _run_recoverable(
            open_stream,
            lambda stream: None,
            reset=lambda: calls.append("reset"),
            error_types=(_FakePortAudioError,),
            wait=wait,
            retry_seconds=0.1,
            stop=None,
            max_retries=3,
        )

    assert calls == [
        "open", "reset", "wait:0.1",
        "open", "reset", "wait:0.1",
        "open",  # third consecutive failure hits the ceiling -> raise, no reset/wait
    ]


def test_recovery_stop_event_exits_before_ceiling_reached_no_raise() -> None:
    """A stop request during the retry wait must still exit cleanly (no raise) even
    though the consecutive-failure ceiling was never reached."""
    calls: list[str] = []

    def open_stream():
        calls.append("open")
        raise _FakePortAudioError("boom")

    def wait(seconds: float) -> bool:
        calls.append("wait")
        return True  # stop requested during the wait

    _run_recoverable(
        open_stream,
        lambda stream: None,
        reset=lambda: calls.append("reset"),
        error_types=(_FakePortAudioError,),
        wait=wait,
        retry_seconds=1.0,
        stop=None,
        max_retries=3,
    )

    assert calls == ["open", "reset", "wait"]  # one attempt only — stop wins, no raise


def test_recovery_immediate_read_failures_after_open_still_accumulate_to_ceiling() -> None:
    """Round 3 review: a bare successful open is NOT healthy by itself. If listen()
    fails immediately every time (elapsed << the healthy interval), the failures must
    genuinely accumulate and re-raise at exactly max_retries — not reset to zero on
    every attempt just because open_stream() itself kept succeeding."""
    calls: list[str] = []

    def open_stream():
        calls.append("open")
        return _ok_stream()

    def listen(stream: object) -> None:
        raise _FakePortAudioError("read glitch")  # fails immediately, every time

    fake_clock = {"t": 0.0}

    def now() -> float:
        fake_clock["t"] += 0.01  # far below the 1.0s healthy interval
        return fake_clock["t"]

    with pytest.raises(_FakePortAudioError):
        _run_recoverable(
            open_stream,
            listen,
            reset=lambda: calls.append("reset"),
            error_types=(_FakePortAudioError,),
            wait=lambda seconds: False,
            retry_seconds=0.0,
            stop=None,
            max_retries=3,
            now=now,
        )

    assert calls == [
        "open", "reset",
        "open", "reset",
        "open",  # third consecutive failure hits the ceiling -> raise, no reset/wait
    ]


def test_recovery_resets_failure_budget_after_healthy_interval_elapses() -> None:
    """Round 3 review: if the stream stays operational for at least
    _AUDIO_HEALTHY_SECONDS before failing, that failure starts a FRESH streak (attempt
    1) rather than continuing the prior one — so a genuinely-flaky-but-mostly-working
    stream never hits the ceiling."""
    calls: list[str] = []
    fake_clock = {"t": 0.0}

    def now() -> float:
        return fake_clock["t"]

    def open_stream():
        calls.append("open")
        return _ok_stream()

    attempts = {"n": 0}

    def listen(stream: object) -> None:
        attempts["n"] += 1
        fake_clock["t"] += 2.0  # stream ran well past the 1.0s healthy interval
        if attempts["n"] < 5:
            raise _FakePortAudioError("glitch after running healthy")
        # fifth attempt "runs to completion" (stop implied set)

    _run_recoverable(
        open_stream,
        listen,
        reset=lambda: calls.append("reset"),
        error_types=(_FakePortAudioError,),
        wait=lambda seconds: False,
        retry_seconds=0.0,
        stop=None,
        max_retries=3,  # would have raised by the 3rd failure if the budget never reset
        now=now,
    )

    assert attempts["n"] == 5


def test_recovery_warns_with_accurate_attempt_count_after_a_healthy_reset(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """After a healthy-interval reset, the next warning must report attempt 1/N, not a
    stale count carried over from the prior (reset) streak."""
    fake_clock = {"t": 0.0}

    def now() -> float:
        return fake_clock["t"]

    def open_stream():
        return _ok_stream()

    attempts = {"n": 0}

    def listen(stream: object) -> None:
        attempts["n"] += 1
        fake_clock["t"] += 2.0  # past the healthy interval every time
        if attempts["n"] == 3:
            return  # stop implied set on the third, healthy, attempt
        raise _FakePortAudioError("glitch after running healthy")

    with caplog.at_level("WARNING", logger="edith.voice.live"):
        _run_recoverable(
            open_stream,
            listen,
            reset=lambda: None,
            error_types=(_FakePortAudioError,),
            wait=lambda seconds: False,
            retry_seconds=0.0,
            stop=None,
            max_retries=3,
            now=now,
        )

    assert attempts["n"] == 3
    warnings = [r.message for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 2
    assert "attempt 1/3" in warnings[0]
    assert "attempt 1/3" in warnings[1]  # each reset independently -> always attempt 1


def test_recovery_default_max_retries_matches_resolver_default() -> None:
    """The default ceiling (no max_retries kwarg) matches resolve_audio_max_retries()'s
    unconfigured default, so a real caller that forgets to pass it still gets a bound."""
    calls = {"opens": 0}

    def open_stream():
        calls["opens"] += 1
        raise _FakePortAudioError("still broken")

    with pytest.raises(_FakePortAudioError):
        _run_recoverable(
            open_stream,
            lambda stream: None,
            reset=lambda: None,
            error_types=(_FakePortAudioError,),
            wait=lambda seconds: False,
            retry_seconds=0.0,
            stop=None,
        )

    assert calls["opens"] == resolve_audio_max_retries()


# ---------------------------------------------------------------------------
# resolve_audio_max_retries — EDITH_AUDIO_MAX_RETRIES override
# ---------------------------------------------------------------------------


def test_resolve_audio_max_retries_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EDITH_AUDIO_MAX_RETRIES", raising=False)
    assert resolve_audio_max_retries() == 5


def test_resolve_audio_max_retries_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EDITH_AUDIO_MAX_RETRIES", "10")
    assert resolve_audio_max_retries() == 10


@pytest.mark.parametrize(
    "raw", ["0", "-1", "-100", "not-a-number", "3.5", "nan", "inf", "-inf", ""]
)
def test_resolve_audio_max_retries_invalid_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv("EDITH_AUDIO_MAX_RETRIES", raw)
    assert resolve_audio_max_retries() == 5


def test_resolve_audio_max_retries_one_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EDITH_AUDIO_MAX_RETRIES", "1")
    assert resolve_audio_max_retries() == 1


# ---------------------------------------------------------------------------
# resolve_audio_retry_seconds — EDITH_AUDIO_RETRY_SECONDS override
# ---------------------------------------------------------------------------


def test_resolve_audio_retry_seconds_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EDITH_AUDIO_RETRY_SECONDS", raising=False)
    assert resolve_audio_retry_seconds() == 1.0


def test_resolve_audio_retry_seconds_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EDITH_AUDIO_RETRY_SECONDS", "2.5")
    assert resolve_audio_retry_seconds() == 2.5


def test_resolve_audio_retry_seconds_invalid_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EDITH_AUDIO_RETRY_SECONDS", "not-a-number")
    assert resolve_audio_retry_seconds() == 1.0


@pytest.mark.parametrize("raw", ["nan", "NaN", "inf", "Infinity", "-inf", "-Infinity"])
def test_resolve_audio_retry_seconds_non_finite_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    """``float("nan")``/``float("inf")`` parse without raising — must be rejected
    explicitly via ``math.isfinite``, not left to slip through as a retry delay."""
    monkeypatch.setenv("EDITH_AUDIO_RETRY_SECONDS", raw)
    assert resolve_audio_retry_seconds() == 1.0


@pytest.mark.parametrize("raw", ["-1", "-0.5", "-1e-9"])
def test_resolve_audio_retry_seconds_negative_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv("EDITH_AUDIO_RETRY_SECONDS", raw)
    assert resolve_audio_retry_seconds() == 1.0


def test_resolve_audio_retry_seconds_zero_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EDITH_AUDIO_RETRY_SECONDS", "0")
    assert resolve_audio_retry_seconds() == 0.0


def test_resolve_audio_retry_seconds_finite_positive_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EDITH_AUDIO_RETRY_SECONDS", "2.5")
    assert resolve_audio_retry_seconds() == 2.5


# ---------------------------------------------------------------------------
# Device override reaches the real stream constructor — no env -> None/default
# ---------------------------------------------------------------------------


class _FakeRawInputStream:
    """Fake ``sounddevice.RawInputStream`` — records ctor kwargs, no hardware."""

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs


class _FakeSdModule:
    RawInputStream = _FakeRawInputStream


def test_open_input_stream_passes_device_through() -> None:
    stream = _open_input_stream(_FakeSdModule(), 2)
    assert isinstance(stream, _FakeRawInputStream)
    assert stream.kwargs["device"] == 2


def test_open_input_stream_none_device_passes_none_through() -> None:
    """No env override -> device=None, the sounddevice default."""
    stream = _open_input_stream(_FakeSdModule(), None)
    assert stream.kwargs["device"] is None


def test_resolve_input_device_unset_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EDITH_INPUT_DEVICE", raising=False)
    assert resolve_input_device() is None


def test_resolve_input_device_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EDITH_INPUT_DEVICE", "0")
    assert resolve_input_device() == 0


# ---------------------------------------------------------------------------
# _sleep_wait — the stop=None fallback (real callers always pass stop.wait)
# ---------------------------------------------------------------------------


def test_sleep_wait_sleeps_and_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []
    monkeypatch.setattr("edith.voice.live.time.sleep", lambda seconds: slept.append(seconds))
    assert _sleep_wait(0.01) is False
    assert slept == [0.01]
