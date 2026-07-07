"""edithd orchestrator — startup order, secrets seam, secure store, shutdown
(spec 01 §"edithd daemon lifecycle").

The orchestrator wires the already-built Bus / Memory / Router / Brain together
and brings up the Control API. Tests inject fakes for the cross-process bits
(secrets provider, Router, a Memory spy) so no real Keychain, no live model
call, no encrypted-volume mount, and no long-lived daemon is touched. The
Control API runs on a short tmp socket (real socket, no mock).

Asserted:
  - the data dir is created 0700 (SecureStore dev impl),
  - startup brings the Control API up and RUNNING; status works over the socket,
  - pause via the Control API ⇒ Brain skips the model call (pause wired from the
    RuntimeState the daemon owns),
  - graceful shutdown (kill): compact() called defensively if present, Memory
    closed, socket removed,
  - secrets provider: keyring-miss falls back to env (deterministic, no Keychain).
"""

from __future__ import annotations

import stat
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest

from edith.daemon.client import ControlClient
from edith.daemon.edithd import EdithDaemon, Secrets, resolve_secrets
from edith.router import ModelResponse, Tier


@pytest.fixture
def data_dir() -> Iterator[Path]:
    # Short path: the Control API socket lives under here and must fit sun_path.
    with tempfile.TemporaryDirectory(dir="/tmp") as d:  # noqa: S108 - short path for sun_path limit
        yield Path(d) / "edithdata"


class FakeRouter:
    def __init__(self) -> None:
        self.calls: list[object] = []

    async def model_call(
        self, messages, tier_hint: Tier, max_tokens: int = 1024  # noqa: ANN001
    ) -> ModelResponse:
        self.calls.append(messages)
        return ModelResponse(text="ok", input_tokens=1, output_tokens=1)


class SpyMemory:
    def __init__(self) -> None:
        self.remembered_nodes: list[object] = []
        self.closed = False
        self.compacted = False

    def recall(self, query: str) -> list[dict[str, object]]:
        return []

    def remember(self, nodes=None, edges=None) -> None:  # noqa: ANN001
        self.remembered_nodes.extend(nodes or [])

    def compact(self) -> None:
        self.compacted = True

    def close(self) -> None:
        self.closed = True


class MemoryNoCompact:
    """A Memory without compact() — mirrors the real MemoryStore (compact deferred)."""

    def recall(self, query: str) -> list[dict[str, object]]:
        return []

    def remember(self, nodes=None, edges=None) -> None:  # noqa: ANN001
        pass

    def close(self) -> None:
        self.closed = True


def _daemon(data_dir: Path, memory, router) -> EdithDaemon:  # noqa: ANN001
    return EdithDaemon(
        data_dir=data_dir,
        secrets=Secrets(bifrost_api_key="k", bifrost_base_url="https://x"),
        memory=memory,
        router=router,
    )


async def test_startup_creates_data_dir_0700(data_dir):
    daemon = _daemon(data_dir, SpyMemory(), FakeRouter())
    await daemon.start()
    try:
        assert data_dir.is_dir()
        assert stat.S_IMODE(data_dir.stat().st_mode) == 0o700
    finally:
        await daemon.stop()


async def test_status_over_socket_after_startup(data_dir):
    daemon = _daemon(data_dir, SpyMemory(), FakeRouter())
    await daemon.start()
    try:
        resp = await ControlClient(daemon.socket_path).send({"cmd": "status"})
    finally:
        await daemon.stop()

    assert resp["ok"] is True
    status = cast(dict[str, object], resp["status"])
    assert status["state"] == "running"
    assert set(status) == {"state", "active_skill", "budget_used", "last_event"}


async def test_pause_via_control_api_makes_brain_skip_model_call(data_dir):
    router = FakeRouter()
    daemon = _daemon(data_dir, SpyMemory(), router)
    await daemon.start()
    try:
        client = ControlClient(daemon.socket_path)
        await client.send({"cmd": "pause"})
        # utterance while paused -> Brain must skip the model call
        await daemon.bus.publish(
            "voice.utterance", source="voice", payload={"text": "hi"}
        )
        assert router.calls == []

        # resume -> the next utterance goes through
        await client.send({"cmd": "resume"})
        await daemon.bus.publish(
            "voice.utterance", source="voice", payload={"text": "hi again"}
        )
        assert len(router.calls) == 1
    finally:
        await daemon.stop()


async def test_pr_review_skill_registered_and_dispatches(data_dir):
    """spec 02 build-step 3: the daemon registers PRReviewSkill, so a matching
    voice.utterance dispatches to it (publishes skill.result) instead of the
    default recall→answer path. Unknown person + SpyMemory ⇒ the skill ASKs and
    never calls the model — proving dispatch, not the answer loop, handled it."""
    router = FakeRouter()
    daemon = _daemon(data_dir, SpyMemory(), router)
    await daemon.start()
    results: list[object] = []

    async def capture(event) -> None:  # noqa: ANN001
        results.append(event.payload)

    daemon.bus.subscribe("skill.result", capture)
    try:
        await daemon.bus.publish(
            "voice.utterance", source="voice", payload={"text": "review Tavishi's PR"}
        )
    finally:
        await daemon.stop()

    assert router.calls == []          # default answer path NOT taken
    assert len(results) == 1           # the skill published its result
    assert results[0]["asked"]         # unknown person ⇒ ASK (no gh, no model)


async def test_injected_resolve_repo_is_wired_to_brain(data_dir):
    """spec 09 realtime resolve-on-miss: edithd passes an injected resolve_repo
    through to Brain, so a repo the graph misses gets resolved live. Verified by
    a miss utterance that names a repo ⇒ the resolver fires."""
    from edith.finder import ResolveResult, ResolveStatus

    calls: list[str] = []

    async def fake_resolver(name: str) -> ResolveResult:
        calls.append(name)
        return ResolveResult(
            ResolveStatus.RESOLVED, name=name, answer="it's a service.", background=None
        )

    daemon = EdithDaemon(
        data_dir=data_dir,
        secrets=Secrets(bifrost_api_key="k", bifrost_base_url="https://x"),
        memory=SpyMemory(),  # recall -> [] (a miss)
        router=FakeRouter(),
        resolve_repo=fake_resolver,
    )
    await daemon.start()
    try:
        await daemon.bus.publish(
            "voice.utterance", source="voice", payload={"text": "what is the widget repo?"}
        )
    finally:
        await daemon.stop()

    assert calls == ["widget"]  # the injected resolver ran on the miss


async def test_real_memorystore_gets_a_default_resolver(data_dir, tmp_path):
    """With a concrete MemoryStore and no injected resolver, edithd builds a
    default one, so the running daemon does realtime repo lookup out of the box.
    A fake Memory (not a MemoryStore) gets no resolver ⇒ existing tests unchanged."""
    from edith.memory.store import MemoryStore

    store = MemoryStore(str(tmp_path / "m.kuzu"))
    daemon = EdithDaemon(
        data_dir=data_dir,
        secrets=Secrets(bifrost_api_key="k", bifrost_base_url="https://x"),
        memory=store,
        router=FakeRouter(),
    )
    await daemon.start()
    try:
        assert daemon._brain is not None
        assert daemon._brain._resolve_repo is not None  # default wired for a real store
    finally:
        await daemon.stop()
        store.close()


async def test_fake_memory_gets_no_default_resolver(data_dir):
    """A non-MemoryStore memory ⇒ resolver stays None (existing behavior)."""
    daemon = _daemon(data_dir, SpyMemory(), FakeRouter())
    await daemon.start()
    try:
        assert daemon._brain is not None
        assert daemon._brain._resolve_repo is None
    finally:
        await daemon.stop()


async def test_kill_shuts_down_gracefully_and_compacts(data_dir):
    memory = SpyMemory()
    daemon = _daemon(data_dir, memory, FakeRouter())
    await daemon.start()
    sock = daemon.socket_path
    await ControlClient(sock).send({"cmd": "kill"})
    # kill triggers graceful shutdown; give it the awaited stop
    await daemon.wait_stopped()

    assert memory.compacted is True  # final compact()
    assert memory.closed is True  # Kuzu closed
    assert not Path(sock).exists()  # socket removed


async def test_shutdown_is_safe_when_memory_has_no_compact(data_dir):
    # compact() is deferred on the real MemoryStore; shutdown must not blow up.
    memory = MemoryNoCompact()
    daemon = _daemon(data_dir, memory, FakeRouter())
    await daemon.start()
    await daemon.stop()
    assert memory.closed is True


def test_resolve_secrets_falls_back_to_env_on_keyring_miss(monkeypatch):
    # keyring returns None (no Keychain entry) -> env vars are used. No Keychain touched.
    monkeypatch.setattr(
        "edith.daemon.edithd.keyring.get_password", lambda service, user: None
    )
    monkeypatch.setenv("BIFROST_API_KEY", "env-key")
    monkeypatch.setenv("BIFROST_BASE_URL", "https://env.example")

    secrets = resolve_secrets()

    assert secrets.bifrost_api_key == "env-key"
    assert secrets.bifrost_base_url == "https://env.example"


def test_resolve_secrets_prefers_keyring(monkeypatch):
    monkeypatch.setattr(
        "edith.daemon.edithd.keyring.get_password",
        lambda service, user: "keychain-key" if "key" in user else "https://kc.example",
    )
    monkeypatch.delenv("BIFROST_API_KEY", raising=False)

    secrets = resolve_secrets()

    assert secrets.bifrost_api_key == "keychain-key"
