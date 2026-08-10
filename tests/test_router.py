"""Router + Bifrost adapter (spec 05 §Bifrost adapter / §Interface to edithd).

``model_call(messages, tier_hint) -> ModelResponse`` over the Anthropic-compatible
Bifrost gateway. HTTP transport is dependency-injected so request construction,
response parsing, tier→model mapping and retry-on-5xx are unit-tested WITHOUT a
live call. Exactly one live smoke test is marked ``live`` (skipped by default).
"""

from __future__ import annotations

import os

import httpx
import pytest

from edith.router import ModelResponse, Router, Tier

_BASE = "https://bifrost.test.internal/anthropic"
_KEY = "sk-bf-TESTKEY-not-real"
_MODELS = {
    Tier.HAIKU: "claude-haiku-4-5-20251001",
    Tier.SONNET: "claude-sonnet-4-6",
    Tier.OPUS: "claude-opus-4-8",
}


def _ok_body(text: str = "hi there") -> dict[str, object]:
    return {
        "content": [{"type": "text", "text": text}],
        "usage": {"input_tokens": 7, "output_tokens": 3},
    }


def _router(handler: httpx.MockTransport) -> Router:
    client = httpx.AsyncClient(transport=handler, base_url=_BASE)
    return Router(client=client, api_key=_KEY, models=_MODELS)


async def test_model_call_parses_text_and_usage():
    async def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ok_body("last time it was the service account"))

    router = _router(httpx.MockTransport(handle))
    resp = await router.model_call([{"role": "user", "content": "why?"}], Tier.SONNET)

    assert isinstance(resp, ModelResponse)
    assert resp.text == "last time it was the service account"
    assert resp.input_tokens == 7
    assert resp.output_tokens == 3


async def test_request_construction_headers_endpoint_body():
    seen: dict[str, object] = {}

    async def handle(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["method"] = request.method
        seen["x-api-key"] = request.headers.get("x-api-key")
        seen["anthropic-version"] = request.headers.get("anthropic-version")
        seen["content-type"] = request.headers.get("content-type")
        import json

        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_ok_body())

    router = _router(httpx.MockTransport(handle))
    await router.model_call([{"role": "user", "content": "ping"}], Tier.HAIKU)

    assert seen["method"] == "POST"
    assert seen["url"] == f"{_BASE}/v1/messages"
    assert seen["x-api-key"] == _KEY
    assert seen["anthropic-version"] == "2023-06-01"
    assert seen["content-type"] == "application/json"
    body = seen["body"]
    assert isinstance(body, dict)
    assert body["model"] == "claude-haiku-4-5-20251001"
    assert body["messages"] == [{"role": "user", "content": "ping"}]
    assert isinstance(body["max_tokens"], int)


@pytest.mark.parametrize(
    "tier,expected",
    [
        (Tier.HAIKU, "claude-haiku-4-5-20251001"),
        (Tier.SONNET, "claude-sonnet-4-6"),
        (Tier.OPUS, "claude-opus-4-8"),
    ],
)
async def test_tier_maps_to_model(tier: Tier, expected: str):
    seen_model: dict[str, object] = {}

    async def handle(request: httpx.Request) -> httpx.Response:
        import json

        seen_model["model"] = json.loads(request.content)["model"]
        return httpx.Response(200, json=_ok_body())

    router = _router(httpx.MockTransport(handle))
    await router.model_call([{"role": "user", "content": "x"}], tier)
    assert seen_model["model"] == expected


async def test_retries_on_5xx_then_succeeds():
    calls = {"n": 0}

    async def handle(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, json={"error": "overloaded"})
        return httpx.Response(200, json=_ok_body("recovered"))

    router = _router(httpx.MockTransport(handle))
    resp = await router.model_call([{"role": "user", "content": "x"}], Tier.SONNET)

    assert calls["n"] == 2  # one retry
    assert resp.text == "recovered"


async def test_does_not_retry_on_4xx():
    calls = {"n": 0}

    async def handle(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, json={"error": "bad request"})

    router = _router(httpx.MockTransport(handle))
    with pytest.raises(httpx.HTTPStatusError):
        await router.model_call([{"role": "user", "content": "x"}], Tier.SONNET)

    assert calls["n"] == 1  # no retry on a 4xx


@pytest.mark.live
async def test_live_smoke_real_bifrost_200_nonempty():
    """One real round-trip. Skipped unless --run-live. max_tokens tiny (cost rule)."""
    base = os.environ.get("BIFROST_BASE_URL")
    key = os.environ.get("BIFROST_API_KEY")
    if not base or not key:
        pytest.skip("BIFROST_BASE_URL / BIFROST_API_KEY not set")

    async with httpx.AsyncClient(base_url=base, timeout=30.0) as client:
        router = Router(client=client, api_key=key, models=_MODELS)
        resp = await router.model_call(
            [{"role": "user", "content": "Say hi in one word."}],
            Tier.HAIKU,
            max_tokens=8,
        )
    assert resp.text.strip() != ""


async def test_system_message_is_hoisted_and_marked_as_a_cache_breakpoint():
    """The preamble must ride the top-level ``system`` field, carrying cache_control.

    Sent as ``messages[0]`` it could not carry ``cache_control`` at all, so the preamble
    was re-billed at full input price on every turn. It is also the wrong shape: a
    leading ``role: "system"`` entry is rejected by some models and the gateway was
    quietly normalizing it.
    """
    seen: dict[str, object] = {}

    async def handle(request: httpx.Request) -> httpx.Response:
        import json

        seen.update(json.loads(request.content))
        return httpx.Response(200, json=_ok_body())

    router = _router(httpx.MockTransport(handle))
    await router.model_call(
        [
            {"role": "system", "content": "You are EDITH."},
            {"role": "user", "content": "hello"},
        ],
        Tier.SONNET,
    )

    assert seen["system"] == [
        {
            "type": "text",
            "text": "You are EDITH.",
            "cache_control": {"type": "ephemeral"},
        }
    ]
    # ...and it is no longer duplicated in messages
    assert seen["messages"] == [{"role": "user", "content": "hello"}]


async def test_messages_without_a_leading_system_are_passed_through():
    """No system preamble → no ``system`` field, and messages are untouched."""
    seen: dict[str, object] = {}

    async def handle(request: httpx.Request) -> httpx.Response:
        import json

        seen.update(json.loads(request.content))
        return httpx.Response(200, json=_ok_body())

    router = _router(httpx.MockTransport(handle))
    await router.model_call([{"role": "user", "content": "hello"}], Tier.SONNET)

    assert "system" not in seen
    assert seen["messages"] == [{"role": "user", "content": "hello"}]


async def test_cached_prefix_tokens_are_still_charged_to_the_budget():
    """A cached prefix is reported under cache_* INSTEAD of input_tokens.

    Charging input_tokens alone would bill Guard 14 tokens for a turn that actually
    consumed a 4 800-token preamble — silently under-metering the window the budget
    exists to enforce. Measured live: with a cache breakpoint, input_tokens fell from
    ~4 800 to 14 while the prefix moved into cache_creation/cache_read.
    """
    charged: list[tuple[int, int]] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": "ok"}],
                "usage": {
                    "input_tokens": 14,
                    "output_tokens": 5,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 4818,
                },
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url=_BASE)
    router = Router(
        client=client,
        api_key=_KEY,
        models=_MODELS,
        on_usage=lambda i, o: charged.append((i, o)),
    )
    response = await router.model_call([{"role": "user", "content": "hi"}], Tier.SONNET)

    assert response.input_tokens == 14  # the uncached remainder, reported faithfully
    assert response.cache_read_tokens == 4818
    assert response.billable_input_tokens == 4832
    assert charged == [(4832, 5)], "Guard must see the cached prefix, not just the remainder"
