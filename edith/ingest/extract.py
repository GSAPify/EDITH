"""Model-backed extraction (spec 08 §Model routing).

Two-tier, budget-aware (north-star §6.2, latency-first):

- **Sonnet** (cheap): short summary + relevance classification of a repo's
  redacted docs. Cheap enough to run on every repo.
- **Opus** (deep): purpose / components / stack / owners extraction, run ONLY
  when Sonnet marks the repo relevant enough to be worth it.

The Router is CONSTRUCTOR-INJECTED so unit tests use a deterministic fake — no
live calls. As defence-in-depth the message content is re-sanitized at the
model-call egress (``sanitize_text`` is idempotent); redaction upstream is the
primary choke-point, this guarantees no raw text reaches Bifrost even if a
future caller forgets to redact first.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Protocol

from edith.ingest.fetch import RepoDocs
from edith.memory.secrets import sanitize_text
from edith.router import ModelResponse, Tier

# Repos Sonnet scores at or above this are worth an Opus deep pass.
_RELEVANCE_THRESHOLD = 0.4

_SUMMARY_MAX_TOKENS = 256
_DEEP_MAX_TOKENS = 512

_SUMMARY_SYSTEM = (
    "You classify a code repository from its README and CLAUDE.md. Reply with "
    "compact JSON: {\"summary\": <one sentence>, \"relevance\": <0..1 float>}. "
    "Relevance is how central this repo is to the owner's work."
)
_DEEP_SYSTEM = (
    "You deeply extract structured knowledge from a repository's docs. Reply "
    "with compact JSON: {\"purpose\": str, \"components\": [str], "
    "\"stack\": [str], \"owners\": [str]}."
)


class RouterLike(Protocol):
    """The slice of the Router contract extraction uses (spec 05 §4.3)."""

    async def model_call(
        self,
        messages: list[dict[str, object]],
        tier_hint: Tier,
        max_tokens: int = ...,
    ) -> ModelResponse: ...


@dataclass(frozen=True)
class Extraction:
    """Structured knowledge extracted from one repo's redacted docs."""

    summary: str
    relevance: float
    purpose: str = ""
    components: list[str] = field(default_factory=list)
    stack: list[str] = field(default_factory=list)
    owners: list[str] = field(default_factory=list)
    deep: bool = False


def _docs_blob(docs: RepoDocs) -> str:
    parts = [f"# repo: {docs.name}"]
    if docs.metadata:
        parts.append(f"metadata: {json.dumps(docs.metadata, ensure_ascii=False)}")
    if docs.readme:
        parts.append(f"## README\n{docs.readme}")
    if docs.claude_md:
        parts.append(f"## CLAUDE.md\n{docs.claude_md}")
    return "\n\n".join(parts)


async def extract_repo(
    router: RouterLike,
    docs: RepoDocs,
    *,
    relevance_threshold: float = _RELEVANCE_THRESHOLD,
    deep_max_tokens: int = _DEEP_MAX_TOKENS,
) -> Extraction:
    """Sonnet-classify then Opus-deep-extract (only if relevant enough).

    ``docs`` is expected already-redacted; message content is re-sanitized here
    as a hard guarantee before it leaves the process.
    """
    blob = _docs_blob(docs)
    summary_resp = await _call(
        router, _SUMMARY_SYSTEM, blob, Tier.SONNET, _SUMMARY_MAX_TOKENS
    )
    summary, relevance = _parse_summary(summary_resp.text)

    if relevance < relevance_threshold:
        return Extraction(summary=summary, relevance=relevance, deep=False)

    deep_resp = await _call(router, _DEEP_SYSTEM, blob, Tier.OPUS, deep_max_tokens)
    purpose, components, stack, owners = _parse_deep(deep_resp.text)
    return Extraction(
        summary=summary,
        relevance=relevance,
        purpose=purpose,
        components=components,
        stack=stack,
        owners=owners,
        deep=True,
    )


async def _call(
    router: RouterLike, system: str, blob: str, tier: Tier, max_tokens: int
) -> ModelResponse:
    # Egress redaction: idempotent, guarantees no raw secret leaves the process.
    content = sanitize_text(f"{system}\n\n{blob}")
    messages: list[dict[str, object]] = [{"role": "user", "content": content}]
    return await router.model_call(messages, tier, max_tokens=max_tokens)


def _parse_summary(text: str) -> tuple[str, float]:
    data = _loads(text)
    summary = str(data.get("summary", "")).strip()
    raw_relevance = data.get("relevance", 0.0)
    try:
        relevance = float(raw_relevance)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        relevance = 0.0
    return summary, max(0.0, min(1.0, relevance))


def _parse_deep(text: str) -> tuple[str, list[str], list[str], list[str]]:
    data = _loads(text)
    purpose = str(data.get("purpose", "")).strip()
    return (
        purpose,
        _str_list(data.get("components")),
        _str_list(data.get("stack")),
        _str_list(data.get("owners")),
    )


def _loads(text: str) -> dict[str, object]:
    """Parse a JSON object from a model reply, tolerating surrounding prose."""
    stripped = text.strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end < start:
        return {}
    try:
        parsed = json.loads(stripped[start : end + 1])
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _str_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if str(v).strip()]
