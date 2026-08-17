"""Tier selection (spec 05 §Tier selection). Pure logic — no HTTP, no models.

Routing philosophy (revised Session 3, latency-first): Sonnet is EDITH's live voice;
Haiku is instant acks/lookups; Opus is explicitly-invoked/background deep work and never
silently blocks a live turn. These tests pin the hint handling + override rules.
"""

from __future__ import annotations

from edith.router import Tier
from edith.router.tiers import TaskType, estimate_tokens, resolve_tier


def test_explicit_hints_pass_through() -> None:
    assert resolve_tier(Tier.HAIKU).tier is Tier.HAIKU
    assert resolve_tier(Tier.SONNET).tier is Tier.SONNET
    assert resolve_tier(Tier.OPUS).tier is Tier.OPUS  # budget allows by default


def test_haiku_escalates_to_sonnet_on_large_input() -> None:
    # Override: a HAIKU hint with too many tokens is promoted (haiku can't do the job).
    d = resolve_tier(Tier.HAIKU, token_count=5000)
    assert d.tier is Tier.SONNET


def test_ack_filler_demotes_any_tier_to_haiku() -> None:
    # Demotion wins: a spoken filler is always cheap regardless of the hint.
    assert resolve_tier(Tier.OPUS, task_type=TaskType.ACK_FILLER).tier is Tier.HAIKU
    assert resolve_tier(Tier.SONNET, task_type=TaskType.ACK_FILLER).tier is Tier.HAIKU


def test_opus_is_budget_gated_and_falls_back_to_sonnet() -> None:
    d = resolve_tier(Tier.OPUS, budget_allows_opus=False)
    assert d.tier is Tier.SONNET
    assert d.budget_limited is True


def test_opus_allowed_when_budget_ok() -> None:
    d = resolve_tier(Tier.OPUS, budget_allows_opus=True)
    assert d.tier is Tier.OPUS
    assert d.budget_limited is False


def test_default_hint_none_is_sonnet_the_live_voice() -> None:
    assert resolve_tier(None).tier is Tier.SONNET


def test_default_none_small_lookup_is_haiku() -> None:
    d = resolve_tier(None, task_type=TaskType.LOOKUP, token_count=100)
    assert d.tier is Tier.HAIKU


def test_default_none_deep_signal_stays_sonnet_but_suggests_background() -> None:
    # Latency-first: a hard/deep turn does NOT block on opus live — Sonnet holds the
    # turn and the deep work is flagged for a background opus job (think_async, deferred).
    d = resolve_tier(None, task_type=TaskType.CODE_REVIEW)
    assert d.tier is Tier.SONNET
    assert d.suggest_background is True

    d2 = resolve_tier(None, token_count=8000)
    assert d2.tier is Tier.SONNET
    assert d2.suggest_background is True


def test_estimate_tokens_is_roughly_chars_over_four() -> None:
    msgs: list[dict[str, object]] = [{"role": "user", "content": "a" * 400}]
    n = estimate_tokens(msgs)
    assert 80 <= n <= 140  # ~100 tokens for 400 chars


def test_ack_filler_never_suggests_background() -> None:
    d = resolve_tier(Tier.SONNET, task_type=TaskType.ACK_FILLER)
    assert d.suggest_background is False


def test_resolve_models_defaults_to_the_current_generation() -> None:
    """The tier->model map is the ONE place model ids live.

    It used to be duplicated in edith/daemon/__main__.py and edith/voice/__main__.py behind a
    TODO(config) flagging drift — and the two did drift in practice, since a bump had to be
    applied in lockstep with nothing enforcing it. Ids carry no date suffix; the gateway
    resolves the alias to the current dated snapshot.
    """
    import os

    from edith.router import Tier, resolve_models

    for var in ("BIFROST_MODEL_HAIKU", "BIFROST_MODEL_SONNET", "BIFROST_MODEL_OPUS"):
        os.environ.pop(var, None)

    models = resolve_models()
    assert models[Tier.SONNET] == "claude-sonnet-5"  # the live talking voice
    assert models[Tier.OPUS] == "claude-opus-5"  # depth + background reasoning
    assert models[Tier.HAIKU] == "claude-haiku-4-5"
    assert not any("-202" in m for m in models.values()), "model ids take no date suffix"


def test_resolve_models_honors_env_overrides(monkeypatch) -> None:
    """A tier can still be pinned without a code change."""
    from edith.router import Tier, resolve_models

    monkeypatch.setenv("BIFROST_MODEL_OPUS", "claude-opus-4-8")
    assert resolve_models()[Tier.OPUS] == "claude-opus-4-8"
