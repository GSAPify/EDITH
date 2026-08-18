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

import asyncio
import contextlib
import stat
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest

from edith.daemon.client import ControlClient
from edith.daemon.edithd import EdithDaemon, Secrets, resolve_secrets
from edith.daemon.state import VoiceHealth
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
    # LOCKED shape — exactly the original four keys; voice_health moved to the
    # opt-in `status_v2` command (round 4 review) — see test_status_v2_over_socket_*.
    assert set(status) == {"state", "active_skill", "budget_used", "last_event"}


async def test_status_v2_over_socket_after_startup(data_dir):
    daemon = _daemon(data_dir, SpyMemory(), FakeRouter())
    await daemon.start()
    try:
        resp = await ControlClient(daemon.socket_path).send({"cmd": "status_v2"})
    finally:
        await daemon.stop()

    assert resp["ok"] is True
    status = cast(dict[str, object], resp["status"])
    assert status["state"] == "running"
    assert set(status) == {
        "state", "active_skill", "budget_used", "last_event", "voice_health",
    }
    assert status["voice_health"] == "healthy"


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
    results: list[dict[str, object]] = []

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


async def test_session_bus_wired_feeds_last_event(data_dir):
    # Spec 04 §Step 5: a normalized session event surfaces in Control API status.
    daemon = _daemon(data_dir, SpyMemory(), FakeRouter())
    await daemon.start()
    try:
        assert daemon._session_bus is not None
        await daemon._session_bus.ingest(
            {
                "type": "user",
                "sessionId": "sess-xyz",
                "cwd": "/Users/x/gitstuff/agents",
                "gitBranch": "master",
                "promptSource": "typed",
                "message": {"role": "user", "content": "fix the failing dag"},
            }
        )
        resp = await ControlClient(daemon.socket_path).send({"cmd": "status"})
    finally:
        await daemon.stop()

    status = cast(dict[str, object], resp["status"])
    assert status["last_event"] is not None
    assert "prompt" in str(status["last_event"])


async def test_session_query_skill_is_dispatchable(data_dir):
    # Spec 04 §Step 4: an owner question about sessions is answered via Brain dispatch.
    daemon = _daemon(data_dir, SpyMemory(), FakeRouter())
    await daemon.start()
    results: list[dict] = []

    async def _capture(event) -> None:  # noqa: ANN001
        results.append(event.payload)

    daemon.bus.subscribe("skill.result", _capture)
    try:
        assert daemon._session_bus is not None
        await daemon._session_bus.ingest(
            {
                "type": "user",
                "sessionId": "sess-xyz",
                "cwd": "/Users/x/gitstuff/agents",
                "gitBranch": "master",
                "promptSource": "typed",
                "message": {"role": "user", "content": "run tests"},
            }
        )
        await daemon.bus.publish(
            "voice.utterance", source="test", payload={"text": "what's running?"}
        )
    finally:
        await daemon.stop()

    assert len(results) == 1  # the session_query skill handled the turn


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


def test_secrets_repr_does_not_expose_the_api_key():
    # dataclass's default __repr__ prints every field's value -- a bare log/print/f-string
    # of a Secrets instance (or anything holding one) must not leak the key.
    secrets = Secrets(bifrost_api_key="super-secret-key", bifrost_base_url="https://x")

    assert "super-secret-key" not in repr(secrets)


# --- weekly graph refresh (spec 08 item 4) ---------------------------------------------------
#
# The real refresh (ingest_workspace + backfill_embeddings) needs network/`gh` and a real
# Kuzu handle, so these tests inject the interval AND the refresh callable — no real clock
# wait, no network, and never the owner's real ~/.edith/data/memory.kuzu.

async def _wait_until(predicate, timeout: float = 2.0) -> None:  # noqa: ANN001
    """Poll ``predicate`` until true (or raise) — avoids a fixed guessed sleep."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("timed out waiting for condition")
        await asyncio.sleep(0.005)


async def test_graph_refresh_off_by_default(data_dir):
    # No enable_graph_refresh kwarg at all -> existing daemons/tests are unaffected.
    daemon = _daemon(data_dir, SpyMemory(), FakeRouter())
    await daemon.start()
    try:
        assert daemon._graph_refresh_task is None
    finally:
        await daemon.stop()


async def test_graph_refresh_runs_the_injected_callable_on_interval(data_dir):
    calls = {"n": 0}

    def fake_refresh() -> None:
        calls["n"] += 1

    daemon = EdithDaemon(
        data_dir=data_dir,
        secrets=Secrets(bifrost_api_key="k", bifrost_base_url="https://x"),
        memory=SpyMemory(),
        router=FakeRouter(),
        enable_graph_refresh=True,
        graph_refresh_interval_seconds=0.01,
        graph_refresh_fn=fake_refresh,
    )
    await daemon.start()
    try:
        assert daemon._graph_refresh_task is not None
        await _wait_until(lambda: calls["n"] >= 1)
    finally:
        await daemon.stop()


async def test_graph_refresh_skips_a_cycle_while_paused(data_dir):
    calls = {"n": 0}

    def fake_refresh() -> None:
        calls["n"] += 1

    daemon = EdithDaemon(
        data_dir=data_dir,
        secrets=Secrets(bifrost_api_key="k", bifrost_base_url="https://x"),
        memory=SpyMemory(),
        router=FakeRouter(),
        enable_graph_refresh=True,
        graph_refresh_interval_seconds=0.01,
        graph_refresh_fn=fake_refresh,
    )
    daemon.state.pause()
    await daemon.start()
    try:
        await asyncio.sleep(0.05)  # several intervals elapse while paused
        assert calls["n"] == 0  # respected pause: never started a refresh

        daemon.state.resume()
        await _wait_until(lambda: calls["n"] >= 1)
    finally:
        await daemon.stop()


async def test_graph_refresh_in_progress_makes_brain_skip_a_turn(data_dir):
    """The concurrency answer (spec 08 item 4): while the refresh writes Memory from a
    worker thread, Brain must not run recall/remember on the loop thread against the SAME
    handle. Proven by a refresh_fn that blocks until released, mid-refresh."""
    started = threading.Event()
    release = threading.Event()

    def blocking_refresh() -> None:
        started.set()
        release.wait(timeout=5)

    router = FakeRouter()
    memory = SpyMemory()
    daemon = EdithDaemon(
        data_dir=data_dir,
        secrets=Secrets(bifrost_api_key="k", bifrost_base_url="https://x"),
        memory=memory,
        router=router,
        enable_graph_refresh=True,
        graph_refresh_interval_seconds=0.01,
        graph_refresh_fn=blocking_refresh,
    )
    await daemon.start()
    try:
        await _wait_until(lambda: started.is_set())
        assert daemon._graph_refresh_in_progress is True

        await daemon.bus.publish(
            "voice.utterance", source="voice", payload={"text": "hi during refresh"}
        )
        assert router.calls == []              # Brain skipped the turn — refresh was in flight
        assert memory.remembered_nodes == []    # the invariant that actually matters: no write

        release.set()  # let the refresh finish
        await _wait_until(lambda: daemon._graph_refresh_in_progress is False)

        await daemon.bus.publish(
            "voice.utterance", source="voice", payload={"text": "hi after refresh"}
        )
        assert len(router.calls) == 1  # back to normal once the refresh clears
    finally:
        release.set()
        await daemon.stop()


async def test_graph_refresh_error_does_not_kill_the_loop(data_dir):
    """A declared, expected failure (e.g. `gh` missing / a Kuzu write error) must not
    permanently end the weekly loop — the next scheduled cycle still runs (spec 08 item 4)."""
    calls = {"n": 0}

    def flaky_refresh() -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated transient failure")

    daemon = EdithDaemon(
        data_dir=data_dir,
        secrets=Secrets(bifrost_api_key="k", bifrost_base_url="https://x"),
        memory=SpyMemory(),
        router=FakeRouter(),
        enable_graph_refresh=True,
        graph_refresh_interval_seconds=0.01,
        graph_refresh_fn=flaky_refresh,
    )
    await daemon.start()
    try:
        await _wait_until(lambda: calls["n"] >= 2)   # survives the first failure
        assert daemon._graph_refresh_task is not None
        assert not daemon._graph_refresh_task.done()  # the loop task is still alive
    finally:
        await daemon.stop()


async def test_stop_joins_an_in_flight_refresh_before_closing_memory(data_dir):
    """The shutdown correctness fix (spec 08 item 4): stop() must not close Memory while the
    refresh's worker thread is still writing to it. Cancelling the task alone does not stop
    that thread, so stop() waits on the thread's own completion signal first."""
    started = threading.Event()
    release = threading.Event()
    order: list[str] = []

    def blocking_refresh() -> None:
        started.set()
        release.wait(timeout=5)
        order.append("refresh_finished")

    memory = SpyMemory()
    memory.close = lambda: order.append("memory_closed")  # type: ignore[method-assign]

    daemon = EdithDaemon(
        data_dir=data_dir,
        secrets=Secrets(bifrost_api_key="k", bifrost_base_url="https://x"),
        memory=memory,
        router=FakeRouter(),
        enable_graph_refresh=True,
        graph_refresh_interval_seconds=0.01,
        graph_refresh_fn=blocking_refresh,
    )
    await daemon.start()
    await _wait_until(lambda: started.is_set())

    async def _delayed_release() -> None:
        await asyncio.sleep(0.05)
        release.set()

    asyncio.get_running_loop().create_task(_delayed_release())
    await daemon.stop()  # must block until the worker thread actually finishes

    assert order == ["refresh_finished", "memory_closed"]  # never reordered


async def test_stop_cancels_the_graph_refresh_task(data_dir):
    daemon = EdithDaemon(
        data_dir=data_dir,
        secrets=Secrets(bifrost_api_key="k", bifrost_base_url="https://x"),
        memory=SpyMemory(),
        router=FakeRouter(),
        enable_graph_refresh=True,
        graph_refresh_interval_seconds=999.0,  # never fires within the test
        graph_refresh_fn=lambda: None,
    )
    await daemon.start()
    task = daemon._graph_refresh_task
    assert task is not None
    await daemon.stop()
    assert daemon._graph_refresh_task is None
    await _wait_until(lambda: task.cancelled())  # cancellation is processed asynchronously


# ---------------------------------------------------------------------------
# _on_voice_task_done — voice-loop observability (bounded resilience fix). The
# PortAudio retry lives inside the audio loop itself (edith.voice.live); this
# callback only makes an ESCAPED, unexpected exception observable via the
# Control API, without respawning. Exercised directly against manually-built
# tasks — no mic, no daemon start()/stop() needed.
# ---------------------------------------------------------------------------


async def test_voice_task_exception_logs_and_sets_last_event_voice_failed(data_dir):
    daemon = _daemon(data_dir, SpyMemory(), FakeRouter())

    async def _boom() -> None:
        raise RuntimeError("simulated voice loop crash")

    task = asyncio.create_task(_boom())
    with contextlib.suppress(RuntimeError):
        await task

    daemon._on_voice_task_done(task)  # noqa: SLF001

    assert daemon.state.last_event == "voice.failed"
    assert daemon.state.voice_health is VoiceHealth.FAILED


async def test_voice_task_normal_completion_does_not_set_failed(data_dir):
    daemon = _daemon(data_dir, SpyMemory(), FakeRouter())

    async def _ok() -> None:
        return None

    task = asyncio.create_task(_ok())
    await task

    daemon._on_voice_task_done(task)  # noqa: SLF001

    assert daemon.state.last_event != "voice.failed"


async def test_voice_task_cancellation_does_not_set_failed(data_dir):
    daemon = _daemon(data_dir, SpyMemory(), FakeRouter())

    async def _forever() -> None:
        await asyncio.sleep(100)

    task = asyncio.create_task(_forever())
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    daemon._on_voice_task_done(task)  # noqa: SLF001

    assert daemon.state.last_event != "voice.failed"


# ---------------------------------------------------------------------------
# voice_health — sticky voice-loop health (round 2 review). A dedicated field on
# RuntimeState, surfaced via Control API status, that unrelated last_event/
# session/graph-refresh writes must never clear.
# ---------------------------------------------------------------------------


async def test_voice_health_stays_failed_after_unrelated_last_event_writes(data_dir):
    daemon = _daemon(data_dir, SpyMemory(), FakeRouter())

    async def _boom() -> None:
        raise RuntimeError("simulated voice loop crash")

    task = asyncio.create_task(_boom())
    with contextlib.suppress(RuntimeError):
        await task
    daemon._on_voice_task_done(task)  # noqa: SLF001
    assert daemon.state.voice_health is VoiceHealth.FAILED

    # Session narration and graph-refresh both write last_event constantly — neither
    # may clear the sticky voice failure as a side effect.
    daemon.state.last_event = "graph_refresh.started"
    assert daemon.state.voice_health is VoiceHealth.FAILED
    daemon.state.last_event = "session.transition"
    assert daemon.state.voice_health is VoiceHealth.FAILED


async def test_status_v2_over_socket_surfaces_voice_health_after_failure(data_dir):
    daemon = _daemon(data_dir, SpyMemory(), FakeRouter())
    await daemon.start()
    try:
        async def _boom() -> None:
            raise RuntimeError("simulated voice loop crash")

        task = asyncio.create_task(_boom())
        with contextlib.suppress(RuntimeError):
            await task
        daemon._on_voice_task_done(task)  # noqa: SLF001

        resp = await ControlClient(daemon.socket_path).send({"cmd": "status_v2"})
    finally:
        await daemon.stop()

    status = cast(dict[str, object], resp["status"])
    assert status["voice_health"] == "failed"


async def test_start_voice_loop_resets_voice_health_to_healthy(data_dir, monkeypatch):
    """A fresh voice-loop start must reset a sticky failure from a prior run — no
    real audio stack involved (run_live_loop is monkeypatched to a no-op)."""

    async def _fake_run_live_loop(
        voice_io, *, wake_model, wake_threshold, followup_seconds, stop  # noqa: ANN001
    ) -> None:
        await asyncio.sleep(100)  # stays "running" until stop()/cancel tears it down

    monkeypatch.setattr("edith.voice.live.run_live_loop", _fake_run_live_loop)

    class _FakeVoice:
        async def speak(self, text: str) -> None:
            return None

        async def speak_response(self, text: str) -> None:
            return None

        def set_paused(self, paused: bool) -> None:
            return None

    daemon = EdithDaemon(
        data_dir=data_dir,
        secrets=Secrets(bifrost_api_key="k", bifrost_base_url="https://x"),
        memory=SpyMemory(),
        router=FakeRouter(),
        voice=_FakeVoice(),
        enable_voice=True,
    )
    daemon.state.mark_voice_failed()  # simulate a failure from a previous run
    await daemon.start()
    try:
        assert daemon.state.voice_health is VoiceHealth.HEALTHY
    finally:
        await daemon.stop()


# ---------------------------------------------------------------------------
# stop() joining an already-failed voice task must not abort shutdown. The
# done callback above already reported the exception; re-raising it from the
# asyncio.wait_for join would abort Memory/Kuzu close, the reasoner, SecureStore
# and Control API teardown, and skip _stopped.set() — breaking the degraded-
# but-manageable voice-failure contract (Fix round 1).
# ---------------------------------------------------------------------------


async def test_stop_swallows_already_failed_voice_task_and_completes_shutdown(data_dir):
    memory = SpyMemory()
    daemon = _daemon(data_dir, memory, FakeRouter())
    await daemon.start()

    async def _boom() -> None:
        raise RuntimeError("simulated voice loop crash")

    task = asyncio.create_task(_boom())
    with contextlib.suppress(RuntimeError):
        await task
    daemon._on_voice_task_done(task)  # noqa: SLF001 — as the real done-callback would

    # Simulate the voice loop having already been running (and having failed) —
    # no real mic/audio stack involved.
    daemon._voice_task = task  # noqa: SLF001
    daemon._voice_stop = threading.Event()  # noqa: SLF001

    await daemon.stop()  # must not raise the task's RuntimeError

    assert daemon._stopped.is_set()  # noqa: SLF001
    assert memory.closed
    assert daemon._control is None  # noqa: SLF001 — Control API socket cleanup finished
