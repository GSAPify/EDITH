"""Brain's background-reasoning triggers (spec 11).

Two ways Brain fires a background opus job through the injected reasoner:
  - EXPLICIT: an utterance like "think about X" → ack now, background the deep work, skip the
    normal live answer.
  - PASSIVE: a turn whose OWN INPUT is deep (a pasted log / long question — measured on the
    utterance alone, not accumulated context) → answer live on Sonnet AND background a deeper
    opus pass.
On completion the reasoner calls Brain's ``on_done``, which persists the full detail, summarizes
on Sonnet, and pings via ``brain.background_done``.

Fakes only — a fake reasoner records the (messages, on_done) it was handed so the test can drive
the callback itself; a fake router returns a canned response (and can be told to fail).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import httpx

from edith.brain import Brain
from edith.bus import Event, EventBus
from edith.router import BackgroundJob, JobStatus, ModelResponse, Tier

OnDone = Callable[[ModelResponse], Awaitable[None]]

# An input big enough to clear DEEP_TOKENS (4000) on its own: ~4400 tokens of pasted log.
DEEP_INPUT = "error stack frame line " * 800


class FakeRouter:
    def __init__(self, answer: str = "an answer") -> None:
        self.answer = answer
        self.calls: list[tuple[list[dict[str, object]], Tier]] = []
        self.raise_exc: Exception | None = None

    async def model_call(
        self,
        messages: list[dict[str, object]],
        tier_hint: Tier,
        max_tokens: int = 1024,
    ) -> ModelResponse:
        self.calls.append((messages, tier_hint))
        if self.raise_exc is not None:
            raise self.raise_exc
        return ModelResponse(text=self.answer, input_tokens=5, output_tokens=4)


class FakeMemory:
    def __init__(self, recall_hits: list[dict[str, object]] | None = None) -> None:
        self.recall_hits = recall_hits or []
        self.remembered_nodes: list[object] = []

    def recall(self, query: str) -> list[dict[str, object]]:
        return self.recall_hits

    def remember(self, nodes=None, edges=None) -> None:  # noqa: ANN001
        self.remembered_nodes.extend(nodes or [])


class FakeReasoner:
    def __init__(self) -> None:
        self.jobs: list[tuple[list[dict[str, object]], OnDone]] = []

    async def think_async(
        self,
        messages: list[dict[str, object]],
        on_done: OnDone,
        *,
        max_tokens: int = 1024,
    ) -> BackgroundJob:
        self.jobs.append((messages, on_done))
        return BackgroundJob(id="job-test", status=JobStatus.RUNNING)


async def _collect(bus: EventBus, topic: str) -> list[Event]:
    events: list[Event] = []

    async def capture(event: Event) -> None:
        events.append(event)

    bus.subscribe(topic, capture)
    return events


def _remembered_text(memory: FakeMemory) -> str:
    return " ".join(
        str(getattr(n, "props", {}).get("text", "")) for n in memory.remembered_nodes
    )


async def test_explicit_think_about_acks_and_backgrounds_without_a_live_answer() -> None:
    bus = EventBus()
    memory = FakeMemory()
    router = FakeRouter(answer="should-not-be-spoken")
    reasoner = FakeReasoner()
    Brain(bus=bus, memory=memory, router=router, reasoner=reasoner)
    decisions = await _collect(bus, "brain.decision")

    await bus.publish(
        "voice.utterance", source="voice", payload={"text": "think about our sharding strategy"}
    )

    # Background job fired with assembled context that mentions the topic.
    assert len(reasoner.jobs) == 1
    blob = " ".join(str(m.get("content", "")) for m in reasoner.jobs[0][0])
    assert "sharding strategy" in blob
    # A holding ack was spoken; the normal live-answer model call did NOT run.
    assert len(decisions) == 1
    assert router.calls == []
    ack = str(decisions[0].payload["answer"]).lower()
    assert "think" in ack or "ping" in ack


async def test_passive_deep_input_answers_live_then_backgrounds() -> None:
    bus = EventBus()
    memory = FakeMemory()
    router = FakeRouter(answer="a live sonnet answer")
    reasoner = FakeReasoner()
    Brain(bus=bus, memory=memory, router=router, reasoner=reasoner)
    decisions = await _collect(bus, "brain.decision")

    await bus.publish("voice.utterance", source="voice", payload={"text": DEEP_INPUT})

    # Live turn answered normally on Sonnet…
    assert len(router.calls) == 1
    assert router.calls[0][1] is Tier.SONNET
    assert any(d.payload.get("answer") == "a live sonnet answer" for d in decisions)
    # …and because the INPUT itself was deep, a background opus job was also fired.
    assert len(reasoner.jobs) == 1


async def test_normal_short_turn_does_not_background() -> None:
    bus = EventBus()
    memory = FakeMemory()
    router = FakeRouter(answer="quick")
    reasoner = FakeReasoner()
    Brain(bus=bus, memory=memory, router=router, reasoner=reasoner)

    await bus.publish("voice.utterance", source="voice", payload={"text": "what time is it?"})

    assert len(router.calls) == 1  # answered live
    assert reasoner.jobs == []  # nothing backgrounded


async def test_long_accumulated_history_does_not_auto_background_a_trivial_turn() -> None:
    # The cost-safety guarantee: a big conversation must NOT make every trivial turn spawn
    # opus. Recall returns a huge blob (simulating accumulated context), but the utterance
    # itself is short → no background job.
    bus = EventBus()
    memory = FakeMemory(recall_hits=[{"text": "huge recalled context " * 2000}])
    router = FakeRouter(answer="quick")
    reasoner = FakeReasoner()
    Brain(bus=bus, memory=memory, router=router, reasoner=reasoner)

    await bus.publish("voice.utterance", source="voice", payload={"text": "thanks"})

    assert len(router.calls) == 1
    assert reasoner.jobs == []  # trivial utterance ⇒ no opus, despite the large context


async def test_on_done_remembers_summarizes_and_pings() -> None:
    bus = EventBus()
    memory = FakeMemory()
    router = FakeRouter(answer="a short spoken summary")
    reasoner = FakeReasoner()
    Brain(bus=bus, memory=memory, router=router, reasoner=reasoner)
    pings = await _collect(bus, "brain.background_done")

    await bus.publish("voice.utterance", source="voice", payload={"text": "think about X"})
    _messages, on_done = reasoner.jobs[0]
    await on_done(ModelResponse(text="the deep opus conclusion", input_tokens=1, output_tokens=1))

    # A Sonnet summary call was made over the opus result.
    assert router.calls, "on_done should summarize the opus result on Sonnet"
    summary_blob = " ".join(str(m.get("content", "")) for m in router.calls[-1][0])
    assert "the deep opus conclusion" in summary_blob
    # Full detail was persisted.
    assert "the deep opus conclusion" in _remembered_text(memory)
    # The owner was pinged with the short summary on a dedicated event.
    assert len(pings) == 1
    assert pings[0].payload["answer"] == "a short spoken summary"


async def test_summary_failure_still_persists_the_opus_detail() -> None:
    # If the summary model call blips, the expensive opus detail must already be in Memory
    # (persisted BEFORE the summary), and no ping fires (spec 11 §on_done ordering).
    bus = EventBus()
    memory = FakeMemory()
    router = FakeRouter()
    router.raise_exc = httpx.ConnectError("bifrost unreachable")
    reasoner = FakeReasoner()
    Brain(bus=bus, memory=memory, router=router, reasoner=reasoner)
    pings = await _collect(bus, "brain.background_done")

    await bus.publish("voice.utterance", source="voice", payload={"text": "think about X"})
    _messages, on_done = reasoner.jobs[0]
    await on_done(ModelResponse(text="the deep opus conclusion", input_tokens=1, output_tokens=1))

    assert "the deep opus conclusion" in _remembered_text(memory)  # persisted despite failure
    assert pings == []  # summary failed → no spoken ping (but detail is safe)


async def test_what_do_you_think_about_is_a_live_answer_not_a_background_job() -> None:
    # The conversational "what do you think about X" wants a live opinion, not a deferred deep
    # dive — the imperative trigger must not fire on it (spec 11 §explicit trigger).
    bus = EventBus()
    memory = FakeMemory()
    router = FakeRouter(answer="I'd lean toward option A")
    reasoner = FakeReasoner()
    Brain(bus=bus, memory=memory, router=router, reasoner=reasoner)
    decisions = await _collect(bus, "brain.decision")

    await bus.publish(
        "voice.utterance", source="voice", payload={"text": "what do you think about option A?"}
    )

    assert reasoner.jobs == []  # NOT backgrounded
    assert len(router.calls) == 1  # answered live
    assert any(d.payload.get("answer") == "I'd lean toward option A" for d in decisions)


async def test_no_reasoner_treats_think_about_as_a_normal_turn() -> None:
    bus = EventBus()
    memory = FakeMemory()
    router = FakeRouter(answer="normal answer")
    Brain(bus=bus, memory=memory, router=router)  # no reasoner injected
    decisions = await _collect(bus, "brain.decision")

    await bus.publish("voice.utterance", source="voice", payload={"text": "think about X"})

    # No reasoner → the explicit phrase falls through to the ordinary recall→answer path.
    assert len(router.calls) == 1
    assert any(d.payload.get("answer") == "normal answer" for d in decisions)
