"""Brain composition-root additions (spec 10): injectable persona, token cap, error seam.

Headless, fakes only. Mirrors the fake idiom in tests/test_brain_skill_dispatch.py.
"""

from __future__ import annotations

import httpx

from edith.brain import Brain
from edith.brain.loop import _MODEL_ERROR_REPLY
from edith.bus import Event, EventBus
from edith.router import ModelResponse, Tier

_SENTINEL_MAX = -1  # the fake's default; if Brain omits max_tokens, this is what it sees


class RecordingRouter:
    """Records the messages / tier / max_tokens of each call; can be made to raise."""

    def __init__(self, *, raises: Exception | None = None) -> None:
        self.messages: list[list[dict[str, object]]] = []
        self.max_tokens: list[int] = []
        self._raises = raises

    async def model_call(
        self,
        messages: list[dict[str, object]],
        tier_hint: Tier,
        max_tokens: int = _SENTINEL_MAX,
    ) -> ModelResponse:
        self.messages.append(messages)
        self.max_tokens.append(max_tokens)
        if self._raises is not None:
            raise self._raises
        return ModelResponse(text="the answer", input_tokens=1, output_tokens=1)


class SpyMemory:
    def __init__(self) -> None:
        self.remembered = 0

    def recall(self, query: str) -> list[dict[str, object]]:
        return []

    def remember(self, nodes=None, edges=None) -> None:  # noqa: ANN001
        self.remembered += 1


async def _fire(bus: EventBus, text: str) -> list[Event]:
    decisions: list[Event] = []

    async def cap(event: Event) -> None:
        decisions.append(event)

    bus.subscribe("brain.decision", cap)
    await bus.publish("voice.utterance", source="voice", payload={"text": text})
    return decisions


def _system_of(router: RecordingRouter) -> str:
    return str(router.messages[0][0]["content"])


async def test_injected_persona_reaches_the_model() -> None:
    bus = EventBus()
    router = RecordingRouter()
    Brain(bus=bus, memory=SpyMemory(), router=router, system_preamble="JARVIS-PERSONA-XYZ")
    await _fire(bus, "what is the weather")
    assert "JARVIS-PERSONA-XYZ" in _system_of(router)


async def test_default_persona_when_none_injected() -> None:
    bus = EventBus()
    router = RecordingRouter()
    Brain(bus=bus, memory=SpyMemory(), router=router)  # no preamble
    await _fire(bus, "what is the weather")
    assert "always-on local assistant" in _system_of(router)  # the default preamble


async def test_answer_max_tokens_is_applied_when_set() -> None:
    bus = EventBus()
    router = RecordingRouter()
    Brain(bus=bus, memory=SpyMemory(), router=router, answer_max_tokens=120)
    await _fire(bus, "hello")
    assert router.max_tokens == [120]


async def test_no_cap_leaves_router_default() -> None:
    bus = EventBus()
    router = RecordingRouter()
    Brain(bus=bus, memory=SpyMemory(), router=router)  # no cap
    await _fire(bus, "hello")
    assert router.max_tokens == [_SENTINEL_MAX]  # Brain did NOT override max_tokens


async def test_model_error_speaks_apology_and_does_not_remember() -> None:
    """A transport failure publishes a graceful fallback decision, not silence,
    and does NOT persist the failed exchange."""
    bus = EventBus()
    memory = SpyMemory()
    router = RecordingRouter(raises=httpx.ConnectError("boom"))  # an httpx.HTTPError
    Brain(bus=bus, memory=memory, router=router)
    decisions = await _fire(bus, "tell me something")

    assert len(decisions) == 1
    assert decisions[0].payload["answer"] == _MODEL_ERROR_REPLY
    assert memory.remembered == 0  # apology is not written to memory


async def test_timeout_error_is_also_caught() -> None:
    bus = EventBus()
    router = RecordingRouter(raises=TimeoutError("slow"))
    Brain(bus=bus, memory=SpyMemory(), router=router)
    decisions = await _fire(bus, "hi")
    assert decisions[0].payload["answer"] == _MODEL_ERROR_REPLY
