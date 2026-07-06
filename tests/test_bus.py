"""Event bus — in-process async pub/sub (north-star §4.1).

The bus is the backbone every component publishes/subscribes on. Envelope is
the north-star shape ``{topic, ts, source, payload}``. These tests fix the
observable contract: subscribe, publish, multiple subscribers, topic filtering.
"""

from __future__ import annotations

from edith.bus import Event, EventBus


async def test_publish_delivers_to_subscriber():
    bus = EventBus()
    received: list[Event] = []

    async def handler(event: Event) -> None:
        received.append(event)

    bus.subscribe("voice.utterance", handler)
    await bus.publish("voice.utterance", source="voice", payload={"text": "hi"})

    assert len(received) == 1
    assert received[0].topic == "voice.utterance"
    assert received[0].source == "voice"
    assert received[0].payload == {"text": "hi"}
    assert received[0].ts > 0


async def test_multiple_subscribers_all_receive():
    bus = EventBus()
    a: list[Event] = []
    b: list[Event] = []

    async def ha(e: Event) -> None:
        a.append(e)

    async def hb(e: Event) -> None:
        b.append(e)

    bus.subscribe("brain.decision", ha)
    bus.subscribe("brain.decision", hb)
    await bus.publish("brain.decision", source="brain", payload={"action": "answer"})

    assert len(a) == 1
    assert len(b) == 1


async def test_topic_filtering_isolates_subscribers():
    bus = EventBus()
    utterances: list[Event] = []
    decisions: list[Event] = []

    async def on_utterance(e: Event) -> None:
        utterances.append(e)

    async def on_decision(e: Event) -> None:
        decisions.append(e)

    bus.subscribe("voice.utterance", on_utterance)
    bus.subscribe("brain.decision", on_decision)

    await bus.publish("voice.utterance", source="voice", payload={"text": "hi"})

    assert len(utterances) == 1
    assert len(decisions) == 0


async def test_publish_with_no_subscribers_is_a_noop():
    bus = EventBus()
    # No handler registered for this topic — must not raise.
    await bus.publish("skill.result", source="skill", payload={"ok": True})
