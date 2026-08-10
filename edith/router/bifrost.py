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
regardless of caller. Guard owns the budget contract and edithd injects it into two seams
here (spec 11): ``budget_check`` gates the tier before a call, ``on_usage`` charges the
window after one. Both default to allow/no-op so a Router built without a Guard is unchanged.

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

# The transport errors a ``model_call*`` can raise after tenacity exhausts its
# retries. Exported so callers (Brain, the voice harness) can catch the router's
# declared failure contract without importing httpx themselves — a specific catch,
# not a bare except. httpx.HTTPError covers TransportError + HTTPStatusError.
MODEL_CALL_ERRORS: tuple[type[Exception], ...] = (TimeoutError, httpx.HTTPError)

# Guard seams (north-star §6.1/§6.2). ``budget_check`` gates a tier BEFORE the call;
# ``on_usage`` charges the window AFTER it. Both default to the no-op/allow behaviour so
# a Router built without a Guard behaves exactly as it did before.
Redactor = Callable[[str], str]
BudgetCheck = Callable[[Tier], bool]  # True == this tier is within budget
Usage = Callable[[int, int], None]  # (tokens_in, tokens_out) -> charged to the budget


class ModelResponse:
    """A completed non-streaming model response."""

    def __init__(
        self,
        text: str,
        input_tokens: int,
        output_tokens: int,
        budget_limited: bool = False,
        cache_creation_tokens: int = 0,
        cache_read_tokens: int = 0,
    ) -> None:
        self.text = text
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        # True when opus was wanted but the budget gate denied it → fell back to sonnet.
        self.budget_limited = budget_limited
        # Prompt-cache accounting. The gateway reports a cached prefix under these two
        # fields INSTEAD of input_tokens, so they must be charged too — see
        # ``billable_input_tokens``.
        self.cache_creation_tokens = cache_creation_tokens
        self.cache_read_tokens = cache_read_tokens

    @property
    def billable_input_tokens(self) -> int:
        """Every input token the turn consumed, cached or not — what Guard must charge.

        ``input_tokens`` is only the UNCACHED remainder. Once a prompt-cache breakpoint
        is in play the bulk of the prefix is reported under ``cache_creation_input_tokens``
        or ``cache_read_input_tokens``, so charging ``input_tokens`` alone silently
        under-meters the window: a 4 800-token preamble read from cache bills Guard for
        14. That would quietly break the guarantee the budget exists to provide.
        """
        return self.input_tokens + self.cache_creation_tokens + self.cache_read_tokens


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
        on_usage: Usage = lambda _tokens_in, _tokens_out: None,
    ) -> None:
        self._client = client
        self._api_key = api_key
        self._models = models
        # Guard seams: default allow + the real sanitize_text choke-point + a no-op
        # meter. edithd injects ``guard.budget_check`` / ``guard.record`` here, which is
        # what makes the daemon's token budget actually decrement.
        self._budget_check = budget_check
        self._redact = redactor
        self._on_usage = on_usage

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
        # Cached prefix tokens are charged too — see ModelResponse.billable_input_tokens.
        self._on_usage(response.billable_input_tokens, response.output_tokens)
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

        Usage IS chargeable here: Anthropic SSE reports it across ``message_start``
        (``input_tokens``) and ``message_delta`` (``output_tokens``), which ``_usage_of``
        accumulates — so ``on_usage`` fires once, just before the final chunk.
        """
        safe = self._redact_messages(messages)
        decision = self._resolve(tier_hint, safe, task_type)
        body = {
            "model": self._models[decision.tier],
            "max_tokens": max_tokens,
            **_split_system(safe),
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
        # Charge the stream. A field the gateway omits reads as 0 rather than being
        # guessed at. A caller that abandons the generator mid-stream never reaches
        # here and is therefore not charged (``model_call_masked``'s pump always drains).
        self._on_usage(
            # Cached prefix tokens are reported INSTEAD of input_tokens — charge all three
            # or the budget window silently under-meters (see billable_input_tokens).
            _int_of(usage, "input_tokens")
            + _int_of(usage, "cache_creation_input_tokens")
            + _int_of(usage, "cache_read_input_tokens"),
            _int_of(usage, "output_tokens"),
        )
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
        flight. BOTH calls are charged to ``on_usage`` — not here, but inside the two
        delegates (``model_call`` and ``model_call_stream``); do not add a third charge
        site here or the masked path double-bills. Answer defaults to **Sonnet**: the
        live turn is never blocked on opus
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
            json={"model": model, "max_tokens": max_tokens, **_split_system(messages)},
        )
        response.raise_for_status()
        return response.json()


def _split_system(
    messages: list[dict[str, object]],
) -> dict[str, object]:
    """Hoist a leading ``role: "system"`` message into the top-level ``system`` field,
    marked as a prompt-cache breakpoint.

    Two things are wrong with sending the preamble as ``messages[0]``. It is not the
    shape the Messages API documents — ``system`` is a top-level field, and a
    ``role: "system"`` entry at index 0 is rejected outright by some models (the
    gateway was quietly normalizing it for us). And it cannot carry ``cache_control``,
    so the preamble was re-billed at full input price on every single turn.

    ``cache_control`` here is free when it does nothing: a prefix below the model's
    minimum (512 tokens on opus-5, 1024 on sonnet-5) simply is not cached — no error,
    no write premium, ``cache_creation_input_tokens: 0``. So this is safe to apply
    unconditionally and starts paying by itself once a preamble grows past the
    threshold, rather than needing someone to remember to switch it on.

    Only a *leading* system message is hoisted; anything else is passed through
    untouched, so a mid-conversation system turn keeps its position.
    """
    if not messages or messages[0].get("role") != "system":
        return {"messages": messages}
    content = messages[0].get("content")
    if not isinstance(content, str) or not content:
        return {"messages": messages}
    return {
        "system": [
            {
                "type": "text",
                "text": content,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        "messages": messages[1:],
    }


def _parse_response(data: dict[str, object]) -> ModelResponse:
    content = data.get("content") or []
    text = ""
    if isinstance(content, list) and content:
        first = content[0]
        if isinstance(first, dict):
            text = str(first.get("text", ""))
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    assert isinstance(usage, dict)
    return ModelResponse(
        text=text,
        input_tokens=_int_of(usage, "input_tokens"),
        output_tokens=_int_of(usage, "output_tokens"),
        cache_creation_tokens=_int_of(usage, "cache_creation_input_tokens"),
        cache_read_tokens=_int_of(usage, "cache_read_input_tokens"),
    )


def _int_of(usage: dict[str, object], key: str) -> int:
    """Read an int token count out of an SSE usage dict; 0 when absent or non-numeric."""
    value = usage.get(key)
    return int(value) if isinstance(value, (int, float)) else 0


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
