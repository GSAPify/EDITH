"""The event envelope and the async pub/sub bus itself."""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

# A subscriber: an async callable taking one Event.
Handler = Callable[["Event"], Awaitable[None]]


@dataclass(frozen=True)
class Event:
    """The north-star bus envelope: ``{topic, ts, source, payload}``."""

    topic: str
    source: str
    payload: dict[str, object]
    ts: float = field(default_factory=time.time)


class EventBus:
    """In-process async pub/sub. One instance per ``edithd`` process."""

    def __init__(self) -> None:
        self._handlers: dict[str, list[Handler]] = defaultdict(list)

    def subscribe(self, topic: str, handler: Handler) -> None:
        """Register ``handler`` to receive events published to ``topic``."""
        self._handlers[topic].append(handler)

    async def publish(
        self, topic: str, source: str, payload: dict[str, object]
    ) -> None:
        """Publish an event; await every subscriber on ``topic``.

        Delivery is deterministic — when this returns, all matching handlers
        have completed. No subscribers on the topic is a silent no-op.
        """
        handlers = self._handlers.get(topic)
        if not handlers:
            return
        event = Event(topic=topic, source=source, payload=payload)
        await asyncio.gather(*(handler(event) for handler in handlers))
