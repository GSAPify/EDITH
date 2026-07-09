"""Bifrost adapter + Router (spec 05).

Bifrost is Pattern's Anthropic-compatible gateway. This turns Router's tier→model choice into
an HTTP call:

    POST {BIFROST_BASE_URL}/v1/messages
    headers: x-api-key, anthropic-version: 2023-06-01, content-type: application/json
    body:    {"model", "max_tokens", "messages", "stream"?}
    resp:    .content[0].text, .usage.{input_tokens, output_tokens}   (or an SSE stream)

The ``httpx.AsyncClient`` is dependency-injected so request construction, response parsing,
tier mapping, streaming and retry are unit-tested with ``MockTransport`` — no live call.

Slice 5 adds, over the single-tier passthrough: **tier selection** (``resolve_tier``),
**streaming** (``model_call_stream``), the **two-call latency-masking** mechanism
(``model_call_masked`` — fast ack + slower answer, two separate overlapped calls), a
**budget gate** seam before opus, and the **redaction choke-point**: ``sanitize_text`` runs on
every outbound message inside every ``model_call*`` so a secret can never reach the gateway
regardless of caller. (Guard owns the real redact/budget contracts; those slices are deferred,
so Router injects seams that default to the safe/allow behaviour.)

The non-streaming ``model_call`` POST path is unchanged from slices 1–4 (callers depend on it);
streaming is added alongside, not retrofitted onto it.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from edith.memory.secrets import sanitize_text
from edith.router.tiers import TaskType, Tier, estimate_tokens, resolve_tier

_ANTHROPIC_VERSION = "2023-06-01"
_DEFAULT_MAX_TOKENS = 1024

# Seams for the deferred Guard slice (north-star §6.1/§6.2).
Redactor = Callable[[str], str]
BudgetCheck = Callable[[Tier], bool]  # True == this tier is within budget


class ModelResponse:
    """A completed non-streaming model response."""

    def __init__(
        self,
        text: str,
        input_tokens: int,
        output_tokens: int,
        budget_limited: bool = False,
    ) -> None:
        self.text = text
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        # True when opus was wanted but the budget gate denied it → fell back to sonnet.
        self.budget_limited = budget_limited


class ModelChunk:
    """One streamed token delta. The final chunk carries usage and ``is_final=True``."""

    def __init__(self, token: str, is_final: bool, usage: dict[str, object] | None = None) -> None:
        self.token = token
        self.is_final = is_final
        self.usage = usage


def _is_retryable(exc: BaseException) -> bool:
    """Retry transient network errors and 5xx; never retry a 4xx caller error."""
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return False


class Router:
    """Model-call gateway: tier selection + streaming + latency masking over Bifrost."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        api_key: str,
        models: dict[Tier, str],
        *,
        budget_check: BudgetCheck = lambda _tier: True,
        redactor: Redactor = sanitize_text,
    ) -> None:
        self._client = client
        self._api_key = api_key
        self._models = models
        # Guard seams (deferred slice): default allow + the real sanitize_text choke-point.
        self._budget_check = budget_check
        self._redact = redactor

    async def model_call(
        self,
        messages: list[dict[str, object]],
        tier_hint: Tier,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
        *,
        task_type: TaskType = TaskType.GENERAL,
    ) -> ModelResponse:
        """Non-streaming call. Redacts, resolves the tier, POSTs, returns the response."""
        safe = self._redact_messages(messages)
        decision = self._resolve(tier_hint, safe, task_type)
        data = await self._post_messages(self._models[decision.tier], safe, max_tokens)
        response = _parse_response(data)
        response.budget_limited = decision.budget_limited
        return response

    async def model_call_stream(
        self,
        messages: list[dict[str, object]],
        tier_hint: Tier,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
        *,
        task_type: TaskType = TaskType.GENERAL,
    ) -> AsyncIterator[ModelChunk]:
        """Streaming call: yields text ``ModelChunk``s, then a final chunk with usage.

        VoiceIO can begin TTS on the first token instead of waiting for completion.
        """
        safe = self._redact_messages(messages)
        decision = self._resolve(tier_hint, safe, task_type)
        body = {
            "model": self._models[decision.tier],
            "max_tokens": max_tokens,
            "messages": safe,
            "stream": True,
        }
        usage: dict[str, object] = {}
        async with self._client.stream(
            "POST", "v1/messages", headers=self._headers(), json=body
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                event = _parse_sse_data(line[len("data:"):].strip())
                if event is None:
                    continue
                usage.update(_usage_of(event))
                text = _delta_text(event)
                if text:
                    yield ModelChunk(token=text, is_final=False)
        yield ModelChunk(token="", is_final=True, usage=usage)

    async def model_call_masked(
        self,
        messages: list[dict[str, object]],
        *,
        ack_prompt: str,
        ack_tier: Tier = Tier.HAIKU,
        answer_tier: Tier = Tier.SONNET,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
    ) -> tuple[AsyncIterator[ModelChunk], asyncio.Task[ModelResponse]]:
        """Two-call latency masking: a fast ack stream + a slower answer, both fired NOW.

        Returns ``(ack_stream, answer_task)`` — two SEPARATE calls (two billing events),
        started concurrently so TTS can speak the ack while the real answer is still in
        flight. Answer defaults to **Sonnet**: the live turn is never blocked on opus
        (spec §Tier selection, latency-first). Opus deep work is background (``think_async``,
        deferred). Brain decides when to invoke this; Router provides the mechanism.
        """
        # Answer starts immediately as a task.
        answer_task: asyncio.Task[ModelResponse] = asyncio.create_task(
            self.model_call(messages, answer_tier, max_tokens)
        )
        # Ack streams immediately too — pumped into a queue by a started task so its HTTP
        # request fires without waiting for the consumer to iterate (true overlap).
        queue: asyncio.Queue[ModelChunk | None] = asyncio.Queue()

        async def _pump() -> None:
            try:
                async for chunk in self.model_call_stream(
                    [{"role": "user", "content": ack_prompt}], ack_tier
                ):
                    await queue.put(chunk)
            finally:
                await queue.put(None)  # sentinel: stream done

        asyncio.create_task(_pump())  # noqa: RUF006 - drained via the returned stream

        async def _ack_stream() -> AsyncIterator[ModelChunk]:
            while True:
                chunk = await queue.get()
                if chunk is None:
                    return
                yield chunk

        return _ack_stream(), answer_task

    def _resolve(self, tier_hint, safe, task_type):  # noqa: ANN001
        return resolve_tier(
            tier_hint,
            task_type=task_type,
            token_count=estimate_tokens(safe),
            budget_allows_opus=self._budget_check(Tier.OPUS),
        )

    def _redact_messages(
        self, messages: list[dict[str, object]]
    ) -> list[dict[str, object]]:
        """Redact every string message content — the unbypassable outbound choke-point."""
        out: list[dict[str, object]] = []
        for m in messages:
            content = m.get("content")
            out.append({**m, "content": self._redact(content)} if isinstance(content, str) else m)
        return out

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self._api_key,
            "anthropic-version": _ANTHROPIC_VERSION,
            "content-type": "application/json",
        }

    @retry(
        retry=retry_if_exception(_is_retryable),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.2, max=2.0),
        reraise=True,
    )
    async def _post_messages(
        self,
        model: str,
        messages: list[dict[str, object]],
        max_tokens: int,
    ) -> dict[str, object]:
        response = await self._client.post(
            "v1/messages",
            headers=self._headers(),
            json={"model": model, "max_tokens": max_tokens, "messages": messages},
        )
        response.raise_for_status()
        return response.json()


def _parse_response(data: dict[str, object]) -> ModelResponse:
    content = data.get("content") or []
    text = ""
    if isinstance(content, list) and content:
        first = content[0]
        if isinstance(first, dict):
            text = str(first.get("text", ""))
    usage = data.get("usage") or {}
    input_tokens = int(usage.get("input_tokens", 0)) if isinstance(usage, dict) else 0
    output_tokens = int(usage.get("output_tokens", 0)) if isinstance(usage, dict) else 0
    return ModelResponse(text=text, input_tokens=input_tokens, output_tokens=output_tokens)


def _parse_sse_data(payload: str) -> dict[str, object] | None:
    if not payload or payload == "[DONE]":
        return None
    try:
        obj = json.loads(payload)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _delta_text(event: dict[str, object]) -> str:
    """Pull text from an Anthropic ``content_block_delta`` event."""
    delta = event.get("delta")
    if isinstance(delta, dict) and delta.get("type") == "text_delta":
        return str(delta.get("text", ""))
    return ""


def _usage_of(event: dict[str, object]) -> dict[str, object]:
    """Accumulate usage from ``message_start`` (input) and ``message_delta`` (output)."""
    usage = event.get("usage")
    if isinstance(usage, dict):
        return usage
    message = event.get("message")
    if isinstance(message, dict) and isinstance(message.get("usage"), dict):
        return message["usage"]  # type: ignore[return-value]
    return {}
