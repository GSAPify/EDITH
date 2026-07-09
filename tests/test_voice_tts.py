"""Tests for the TTSHandle protocol and TTSAdapter ABC (spec 03 §TTS adapter interface).

TDD: test_concrete_subclass_instantiates and test_speak_returns_tts_handle drive the
TTSAdapter ABC; test_abstract_adapter_raises_type_error verifies the abstract guard.
"""

from __future__ import annotations

import pytest

from edith.voice.tts import TTSAdapter, TTSHandle

# ---------------------------------------------------------------------------
# Minimal concrete implementations used only in this test module
# ---------------------------------------------------------------------------


class _FakeHandle:
    """Minimal TTSHandle implementation for testing."""

    def stop(self) -> None:
        pass

    def done(self) -> bool:
        return True


class _FakeAdapter(TTSAdapter):
    """Minimal TTSAdapter implementation for testing."""

    def name(self) -> str:
        return "fake"

    async def speak(self, text: str) -> TTSHandle:
        return _FakeHandle()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_abstract_adapter_raises_type_error() -> None:
    """Instantiating the abstract base directly must raise TypeError."""
    with pytest.raises(TypeError):
        TTSAdapter()  # type: ignore[abstract]


async def test_concrete_subclass_instantiates() -> None:
    """A concrete subclass can be instantiated and name() returns a string."""
    adapter = _FakeAdapter()
    assert adapter.name() == "fake"


async def test_speak_returns_tts_handle() -> None:
    """speak() must return an object that satisfies the TTSHandle protocol."""
    adapter = _FakeAdapter()
    handle = await adapter.speak("Awaiting your instructions.")
    assert isinstance(handle, TTSHandle)


async def test_tts_handle_stop_callable() -> None:
    """TTSHandle.stop() must be callable with no arguments."""
    adapter = _FakeAdapter()
    handle = await adapter.speak("test")
    # stop() must not raise
    handle.stop()
