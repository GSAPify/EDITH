"""Bifrost adapter + Router (spec 05 §Bifrost adapter, §Interface to edithd).

Bifrost is Pattern's Anthropic-compatible gateway. This is the thin layer that
turns Router's tier→model choice into an HTTP call:

    POST {BIFROST_BASE_URL}/v1/messages
    headers: x-api-key, anthropic-version: 2023-06-01, content-type: application/json
    body:    {"model", "max_tokens", "messages": [{"role","content"}]}
    resp:    .content[0].text, .usage.{input_tokens, output_tokens}

The ``httpx.AsyncClient`` is dependency-injected so request construction,
response parsing, tier mapping and retry are unit-tested with ``MockTransport``
— no live call. Transient failures (transport errors, 5xx) are retried with
tenacity (exponential backoff); 4xx is a caller error and is raised immediately.

Secrets: the API key is never logged or printed. Redaction of message content
happens in Brain before ``model_call`` in this slice (spec 05 §Open questions
notes the router-side choke-point; that moves here when Guard lands).
"""

from __future__ import annotations

from enum import Enum

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

_ANTHROPIC_VERSION = "2023-06-01"
_DEFAULT_MAX_TOKENS = 1024


class Tier(Enum):
    """Model size class the Router selects (spec 05 §Tier selection)."""

    HAIKU = "haiku"
    SONNET = "sonnet"
    OPUS = "opus"


class ModelResponse:
    """A completed non-streaming model response."""

    def __init__(self, text: str, input_tokens: int, output_tokens: int) -> None:
        self.text = text
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


def _is_retryable(exc: BaseException) -> bool:
    """Retry transient network errors and 5xx; never retry a 4xx caller error."""
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return False


class Router:
    """Single-tier passthrough over the Bifrost adapter (north-star §7)."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        api_key: str,
        models: dict[Tier, str],
    ) -> None:
        self._client = client
        self._api_key = api_key
        self._models = models

    async def model_call(
        self,
        messages: list[dict[str, object]],
        tier_hint: Tier,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
    ) -> ModelResponse:
        """Call Bifrost for ``messages`` at the model mapped from ``tier_hint``."""
        model = self._models[tier_hint]
        data = await self._post_messages(model, messages, max_tokens)
        return _parse_response(data)

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
            headers={
                "x-api-key": self._api_key,
                "anthropic-version": _ANTHROPIC_VERSION,
                "content-type": "application/json",
            },
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
