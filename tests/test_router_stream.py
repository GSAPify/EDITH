"""Streaming + two-call latency masking + redaction choke-point (spec 05).

MockTransport only — no live Bifrost. The masking test proves TRUE overlap (both requests
issued before either stream is drained), which is the property that delivers the latency win;
mere completion-ordering would pass even for a sequential impl.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from edith.router import ModelChunk, Router, Tier

_BASE = "https://bifrost.test.internal/anthropic"
_KEY = "sk-bf-TESTKEY-not-real"
_MODELS = {
    Tier.HAIKU: "claude-haiku-4-5",
    Tier.SONNET: "claude-sonnet-4-6",
    Tier.OPUS: "claude-opus-4-8",
}

pytestmark = pytest.mark.asyncio


def _event(name: str, payload: dict) -> list[str]:
    return [f"event: {name}", "data: " + json.dumps(payload), ""]


def _sse(*texts: str) -> bytes:
    """A minimal Anthropic-style SSE stream: text deltas then a final usage + stop."""
    lines = _event(
        "message_start", {"type": "message_start", "message": {"usage": {"input_tokens": 5}}}
    )
    for t in texts:
        lines += _event(
            "content_block_delta",
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": t}},
        )
    lines += _event("message_delta", {"type": "message_delta", "usage": {"output_tokens": 3}})
    lines += _event("message_stop", {"type": "message_stop"})
    return ("\n".join(lines)).encode()


def _ok_body(text: str = "hi") -> dict:
    return {
        "content": [{"type": "text", "text": text}],
        "usage": {"input_tokens": 2, "output_tokens": 1},
    }


def _router(handler) -> Router:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=_BASE)
    return Router(client, _KEY, _MODELS)


async def test_stream_yields_text_chunks_then_final_usage() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_sse("Hel", "lo"))

    router = _router(handler)
    stream = router.model_call_stream([{"role": "user", "content": "hi"}], Tier.HAIKU)
    chunks = [c async for c in stream]

    texts = [c.token for c in chunks if c.token]
    assert "".join(texts) == "Hello"
    assert chunks[-1].is_final is True
    assert chunks[-1].usage and chunks[-1].usage.get("output_tokens") == 3


async def test_masking_issues_both_requests_before_draining_ack() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        requests.append(body)
        # Streaming ack vs non-streaming answer are distinguishable by the stream flag.
        if '"stream": true' in body or '"stream":true' in body:
            return httpx.Response(200, content=_sse("on ", "it"))
        return httpx.Response(200, json=_ok_body("the real answer"))

    router = _router(handler)
    ack_stream, answer_task = await router.model_call_masked(
        [{"role": "user", "content": "review the PR"}], ack_prompt="say a brief filler"
    )

    # Let the two tasks issue their requests WITHOUT draining the ack stream.
    await asyncio.sleep(0.05)
    assert len(requests) == 2, "both calls must fire concurrently, not ack-then-answer"

    # They are separate objects, and both still work when consumed.
    ack = "".join([c.token async for c in ack_stream if c.token])
    answer = await answer_task
    assert ack == "on it"
    assert answer.text == "the real answer"
    assert isinstance(answer_task, asyncio.Task)


async def test_masking_answer_defaults_to_sonnet_not_opus() -> None:
    # Latency-first: the live answer is Sonnet by default (opus never blocks the turn).
    models_seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json
        body = json.loads(request.content.decode())
        models_seen.append(body["model"])
        if body.get("stream"):
            return httpx.Response(200, content=_sse("ok"))
        return httpx.Response(200, json=_ok_body())

    router = _router(handler)
    ack_stream, answer_task = await router.model_call_masked(
        [{"role": "user", "content": "hi"}], ack_prompt="filler"
    )
    _ = [c async for c in ack_stream]
    await answer_task
    assert _MODELS[Tier.SONNET] in models_seen   # answer used sonnet
    assert _MODELS[Tier.HAIKU] in models_seen     # ack used haiku
    assert _MODELS[Tier.OPUS] not in models_seen  # opus never touched the live turn


async def test_redaction_is_applied_inside_model_call() -> None:
    # The choke-point: a secret in the outbound message never reaches the HTTP body.
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.content.decode())
        return httpx.Response(200, json=_ok_body())

    router = _router(handler)
    leak = "my aws key is AKIAIOSFODNN7EXAMPLE ok"
    await router.model_call([{"role": "user", "content": leak}], Tier.SONNET)

    assert seen and "AKIAIOSFODNN7EXAMPLE" not in seen[0]
    assert "[REDACTED]" in seen[0]


async def test_opus_hint_denied_by_budget_falls_back_and_marks_limited() -> None:
    models_seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json
        models_seen.append(json.loads(request.content.decode())["model"])
        return httpx.Response(200, json=_ok_body())

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=_BASE)
    router = Router(client, _KEY, _MODELS, budget_check=lambda tier: False)  # deny opus

    resp = await router.model_call([{"role": "user", "content": "think hard"}], Tier.OPUS)
    assert models_seen == [_MODELS[Tier.SONNET]]  # fell back to sonnet
    assert resp.budget_limited is True


async def test_model_chunk_is_importable_and_shaped() -> None:
    c = ModelChunk(token="x", is_final=False)
    assert c.token == "x" and c.is_final is False and c.usage is None


@pytest.mark.live
async def test_live_stream_real_bifrost_yields_tokens() -> None:
    """Real streaming round-trip — proves the SSE parser against the ACTUAL Bifrost event
    stream (MockTransport only proves it against our idea of it). Skipped unless --run-live.
    """
    import os

    base = os.environ.get("BIFROST_BASE_URL")
    key = os.environ.get("BIFROST_API_KEY")
    if not base or not key:
        pytest.skip("BIFROST_BASE_URL / BIFROST_API_KEY not set")

    live_models = {Tier.HAIKU: "claude-haiku-4-5-20251001", Tier.SONNET: "claude-sonnet-4-6",
                   Tier.OPUS: "claude-opus-4-8"}
    async with httpx.AsyncClient(base_url=base, timeout=30.0) as client:
        router = Router(client, key, live_models)
        chunks = [
            c async for c in router.model_call_stream(
                [{"role": "user", "content": "Count: one two three."}], Tier.HAIKU, max_tokens=16
            )
        ]
    assert "".join(c.token for c in chunks).strip() != ""  # real tokens arrived
    assert chunks[-1].is_final is True
