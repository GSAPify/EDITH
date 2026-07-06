"""In-process async event bus (north-star §4.1).

Components never call each other directly for events; they ``publish`` and
``subscribe`` here. This keeps subsystems decoupled and the SessionBus/VoiceIO
producers swappable. It is an in-process asyncio pub/sub within the single
``edithd`` process — not a network broker (no Redis/Kafka).

``publish`` awaits every matching handler (via ``asyncio.gather``) rather than
firing tasks off, so delivery is deterministic: after ``await bus.publish(...)``
returns, all subscribers have run. Payloads are already Guard-redacted before
they reach the bus (north-star §6.1); the bus itself does no redaction.
"""

from edith.bus.event_bus import Event, EventBus, Handler

__all__ = ["Event", "EventBus", "Handler"]
