"""edithd orchestrator — the daemon spine (spec 01 §"edithd daemon lifecycle").

Wires the already-built subsystems (Bus / Memory / Router / Brain) together,
brings the Control API up, and owns the RuntimeState and graceful shutdown. It
does NOT rebuild any subsystem — it composes them in the spec's startup order:

  1. fetch secrets (keyring, with a .env/env fallback — the spec-sanctioned dev
     path), held in RAM only,
  2. open the SecureStore (dev impl: enforce a 0700 data dir; the encrypted-APFS
     mount is a seam — see ``securestore.py``),
  3. bring up the bus,
  4. register Memory / Router / Brain subscriptions (Brain subscribes itself and
     reads ``is_paused`` from the RuntimeState),
  5. start the Control API server on the unix socket,
  6. enter RUNNING.

Graceful shutdown on ``kill`` (spec §Shutdown): stop new intents (state ->
STOPPING blocks Brain), a final ``compact()`` if Memory supports it (compact is
deferred on the real MemoryStore — called defensively), close Memory, close the
SecureStore, close the Control API socket, exit.

This module never binds a network port, never auto-loads launchd, and never
mounts a real volume — those are operational steps documented in the plist
template and the Completion Record.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, cast

import keyring
from keyring.errors import KeyringError

from edith.brain import Brain
from edith.brain.history import TurnBuffer
from edith.brain.loop import MemoryLike, ResolveRepoLike, RouterLike
from edith.bus import Event, EventBus
from edith.daemon.control import ControlServer
from edith.daemon.securestore import LocalSecureStore, SecureStore
from edith.daemon.state import RuntimeState
from edith.finder import ResolveResult
from edith.finder import resolve_repo as _resolve_repo_impl
from edith.guard import Guard
from edith.ingest.workspace import ingest_workspace
from edith.memory.store import MemoryStore
from edith.router import BackgroundReasoner, Tier
from edith.session.bus import SessionBus
from edith.session.collector import TranscriptCollector
from edith.session.narrator import Narrator
from edith.skills import DesktopControlSkill, PRReviewSkill, SessionQuerySkill
from edith.voice.persona import VOICE_PERSONA

_KEYRING_SERVICE = "edithd"
_SOCKET_NAME = "edithd.sock"
# Spoken replies are read aloud → cap tight (spec 10 §Brevity; latency compounds
# with endpointing + follow-up windows). Only applied on the voice-wired path.
_VOICE_MAX_TOKENS = 120
# Weekly graph refresh (spec 08 item 4): the two model-free `--workspace` orgs.
# Deep extraction (Opus per repo) is explicitly out of scope — see the Completion Record.
_GRAPH_REFRESH_ORGS: tuple[str, ...] = ("patterninc", "ampmedia")
_GRAPH_REFRESH_INTERVAL_SECONDS = 7 * 24 * 3600.0
# A transient failure (gh missing, one bad `gh` call surviving its own retries, a Kuzu/sqlite
# write error) must not permanently kill the weekly loop — caught so the NEXT interval still
# fires. Anything outside this declared set is a real bug and is allowed to surface.
_GRAPH_REFRESH_ERRORS: tuple[type[Exception], ...] = (
    OSError,                     # e.g. FileNotFoundError — the `gh` binary is missing
    subprocess.SubprocessError,  # CalledProcessError once _gh_list_repos's own retries exhaust
    RuntimeError,                # Kuzu query failures (verified: kuzu raises plain RuntimeError)
    sqlite3.Error,                # sqlite-vec (embedding) write failures
)
# Bounded wait, on shutdown, for an in-flight refresh's WORKER THREAD to actually finish
# writing before compact()/close() touch the same Memory handle (cancelling the awaiting
# task does not stop that thread — see _graph_refresh_loop). A little over the measured
# ~1.3-min full pass.
_GRAPH_REFRESH_SHUTDOWN_JOIN_TIMEOUT = 120.0
# The mic loop checks its stop flag once per ~80 ms frame, so a clean exit is fast. The
# bound exists for a wedged audio read: past it, shutdown falls back to the old
# best-effort cancel rather than hanging.
_VOICE_SHUTDOWN_JOIN_TIMEOUT = 3.0


class VoiceIOLike(Protocol):
    """The slice of VoiceIO that edithd uses (spec 03 §Wiring).

    Mirrors the MemoryLike / RouterLike pattern: edithd depends on this
    interface, not the concrete VoiceIO class, so tests can pass fakes without
    subclassing. A real ``edith.voice.io.VoiceIO`` satisfies this structurally.
    """

    async def speak(self, text: str) -> None: ...

    async def speak_response(self, text: str) -> None: ...

    def set_paused(self, paused: bool) -> None: ...


@dataclass(frozen=True)
class Secrets:
    """The secrets edithd holds in RAM only (never logged, never persisted)."""

    bifrost_api_key: str = field(repr=False)
    bifrost_base_url: str


def resolve_secrets() -> Secrets:
    """Fetch secrets from the Keychain (``keyring``), falling back to env/.env.

    The Keychain is the production source (north-star §5 / §6.1); the ``.env``
    fallback is the spec-sanctioned dev path (``.env.example``). A missing
    Keychain entry (``None``) or a ``KeyringError`` (no backend on a headless
    dev box) falls through to the environment — not a bare except.
    """
    api_key = _from_keyring("bifrost_api_key") or os.environ.get("BIFROST_API_KEY", "")
    base_url = _from_keyring("bifrost_base_url") or os.environ.get(
        "BIFROST_BASE_URL", ""
    )
    return Secrets(bifrost_api_key=api_key, bifrost_base_url=base_url)


def _from_keyring(user: str) -> str | None:
    try:
        return keyring.get_password(_KEYRING_SERVICE, user)
    except KeyringError:
        return None


class EdithDaemon:
    """Composes the subsystems and runs the daemon lifecycle."""

    def __init__(
        self,
        data_dir: str | Path,
        secrets: Secrets,
        memory: MemoryLike,
        router: RouterLike,
        secure_store: SecureStore | None = None,
        guard: Guard | None = None,
        resolve_repo: ResolveRepoLike | None = None,
        voice: VoiceIOLike | None = None,
        enable_session_awareness: bool = False,
        enable_voice: bool = False,
        enable_graph_refresh: bool = False,
        graph_refresh_interval_seconds: float = _GRAPH_REFRESH_INTERVAL_SECONDS,
        graph_refresh_fn: Callable[[], None] | None = None,
        bus: EventBus | None = None,
    ) -> None:
        self._secrets = secrets  # held in RAM only; never logged
        # Realtime resolve-on-miss (spec 09). Injected for tests; when absent
        # and Memory is a concrete MemoryStore, start() builds a default binding
        # so the running daemon does live repo lookup out of the box.
        self._resolve_repo = resolve_repo
        self._memory = memory
        self._router = router
        # Optional VoiceIOLike (spec 03 §Wiring). When provided: speak seam is
        # wired into PRReviewSkill so findings are spoken, and set_paused()
        # mirrors the Control API pause/resume commands. Default None → no
        # audio, all existing behaviour unchanged.
        self._voice: VoiceIOLike | None = voice
        # Session awareness (spec 04). SessionBus is always wired (cheap, no I/O — it
        # feeds the Control API last_event and backs SessionQuerySkill). The live
        # transcript collector + idle-narration loop only spin up when explicitly
        # enabled, so unit tests never tail the owner's real ~/.claude/projects.
        self._enable_session_awareness = enable_session_awareness
        # Run the live mic/wake/STT loop as a background task (spec 10). Gated so
        # unit tests never open a mic; requires a real VoiceIO. Off by default.
        self._enable_voice = enable_voice
        # Weekly graph refresh (spec 08 item 4): model-free `--workspace` passes +
        # a reembed backfill, run as a background task. Off by default so every
        # existing test and the plain daemon are unaffected. Interval + the refresh
        # callable are both injectable so tests never wait a real week or touch the
        # network (see _start_graph_refresh / _graph_refresh_loop).
        self._enable_graph_refresh = enable_graph_refresh
        self._graph_refresh_interval = graph_refresh_interval_seconds
        self._graph_refresh_fn = graph_refresh_fn
        self._graph_refresh_task: asyncio.Task[None] | None = None
        # True only while a refresh is actually writing (not merely scheduled). Folded into
        # Brain's is_paused predicate below so a live turn never runs recall/remember on the
        # loop thread while the refresh writes the SAME Memory handle from a worker thread
        # (see the Completion Record for why this, not a cross-thread lock, was chosen).
        self._graph_refresh_in_progress = False
        # Set by the WORKER THREAD itself (in its own finally), not by the coroutine — so it
        # reflects the thread truly finishing even if the awaiting task was cancelled out from
        # under it (asyncio.to_thread cannot interrupt an already-running thread). stop() waits
        # on this, bounded, before compact()/close() touch the same Memory handle.
        self._graph_refresh_thread_idle = threading.Event()
        self._graph_refresh_thread_idle.set()
        self._store: SecureStore = secure_store or LocalSecureStore(data_dir)
        # ONE Guard per daemon (spec 11): a single shared window is the whole point — a
        # per-subsystem Guard would give each its own budget, which is not a budget. It
        # feeds four seams below (Router is fed by the composition root) plus the Control
        # API's budget_used. NOTE: the composition root MUST pass the same Guard it gave
        # the Router — a Guard built here while the Router keeps its permissive default
        # means the counter never decrements and nothing says so.
        self._guard = guard if guard is not None else Guard()
        self.state = RuntimeState()
        # A caller may inject the bus so a VoiceIO built on the SAME bus can be passed
        # as ``voice`` (the composition root does this — spec 10). Default: our own.
        self.bus = bus or EventBus()
        self._brain: Brain | None = None
        # Background reasoner (spec 13): built in start() from the injected router so a deep
        # turn can fire opus off the live path. Cancelled on shutdown (cancel_all in stop()).
        self._reasoner: BackgroundReasoner | None = None
        self._control: ControlServer | None = None
        self._session_bus: SessionBus | None = None
        self._session_tasks: list[asyncio.Task[None]] = []
        self._voice_task: asyncio.Task[None] | None = None
        self._voice_stop: threading.Event | None = None
        self._stopped = asyncio.Event()

    @property
    def socket_path(self) -> Path:
        return self._store.data_dir / _SOCKET_NAME

    async def start(self) -> None:
        """Startup in the spec's order; ends in RUNNING with the Control API up."""
        # 2. open the SecureStore (ensures the 0700 data dir; encrypted-volume seam).
        self._store.open()

        # 3. bus is already constructed; 4. register subsystem subscriptions.
        #    Brain subscribes itself to voice.utterance and reads is_paused from
        #    the RuntimeState (single source of truth). Pass a predicate, not the
        #    property value, so it re-reads live state on every utterance.
        # Realtime resolve-on-miss (spec 09): use the injected resolver, else
        # build a default one bound to the store+router when Memory is a concrete
        # MemoryStore (a fake in tests is not, so it stays None — behavior
        # unchanged). This is what makes the running daemon do live repo lookup.
        resolver = self._resolve_repo
        if resolver is None and isinstance(self._memory, MemoryStore):
            resolver = self._make_default_resolver(self._memory)

        # Session awareness (spec 04): SessionBus normalizes transcript records onto
        # the bus and feeds the Control API last_event (RuntimeState). Always built.
        self._session_bus = SessionBus(self.bus, runtime_state=self.state)

        # Register skills so a voice.utterance can dispatch them (spec 02
        # build-step 3). When VoiceIO is wired, pass its speak_response seam into
        # each user-triggered skill so an acknowledgement AND a final reply each
        # independently re-arm the follow-up window (spec 03 §Follow-up). Session
        # narration (below) keeps the raw speak seam instead — see _start_session_
        # awareness. Without VoiceIO the default _silent seam keeps the confirm
        # gate safely denied.
        speak = self._voice.speak if self._voice is not None else None
        speak_response = self._voice.speak_response if self._voice is not None else None
        pr_skill = (
            PRReviewSkill(self._router, speak=speak_response)
            if speak_response is not None
            else PRReviewSkill(self._router)
        )
        # SessionQuerySkill answers "what is session 2 doing?" via Brain dispatch
        # (spec 04 §Step 4). Its state provider is bound to the live SessionBus map.
        session_skill = (
            SessionQuerySkill(
                self._session_bus.session_states, router=self._router, speak=speak_response
            )
            if speak_response is not None
            else SessionQuerySkill(self._session_bus.session_states, router=self._router)
        )
        # DesktopControlSkill turns "open Spotify" / "start OMC in <repo>" into real OS
        # actions (spec 06). Registered LAST: its triggers are broad ("open ", "play "),
        # so the more specific pr-review / session skills claim their turns first. Speak
        # feedback when VoiceIO is wired; the router enables the haiku classify fallback.
        # Guard gates it (spec 11): a denylisted OS verb is refused, spoken, and never run.
        desktop_skill = (
            DesktopControlSkill(router=self._router, speak=speak_response, guard=self._guard)
            if speak_response is not None
            else DesktopControlSkill(router=self._router, guard=self._guard)
        )
        # Background reasoner (spec 13): opus deep work that never blocks the live turn. Built
        # from the injected router and gated by Guard — opus is the first thing cut when the
        # budget runs down (Guard reserves the tail for the live voice).
        self._reasoner = BackgroundReasoner(
            self._router, budget_check=self._guard.budget_check
        )

        # On the voice-wired path, give Brain the spoken persona, a tight token cap,
        # and an in-session recent-turns buffer so it answers by voice with cross-turn
        # context (spec 10). Without voice these stay None/default → unchanged.
        voiced = self._voice is not None
        self._brain = Brain(
            bus=self.bus,
            memory=self._memory,
            router=self._router,
            # Skip a pass while PAUSED (privacy) OR STOPPING — the latter makes the
            # docstring's "STOPPING blocks Brain" real and prevents a late mic
            # utterance from running recall/remember against a closing Kuzu handle.
            # OR while a graph refresh is actually writing (spec 08 item 4): Kuzu's
            # Connection is single-writer and not documented safe for concurrent
            # multi-thread use, so a turn on the loop thread must not run recall/
            # remember while the refresh writes the SAME handle from a worker thread.
            is_paused=lambda: (
                self.state.is_paused
                or self.state.is_stopping
                or self._graph_refresh_in_progress
            ),
            resolve_repo=resolver,
            skills=[pr_skill, session_skill, desktop_skill],
            history=TurnBuffer() if voiced else None,
            system_preamble=VOICE_PERSONA if voiced else None,
            answer_max_tokens=_VOICE_MAX_TOKENS if voiced else None,
            reasoner=self._reasoner,
        )
        # Speak-the-decision: Brain publishes brain.decision ONLY on the plain-answer
        # path (a skill that handles a turn publishes skill.result and speaks itself),
        # so this subscriber speaks that path with no double-speak (spec 10 §decision 3).
        if voiced:
            self.bus.subscribe("brain.decision", self._speak_decision)
            # Background-reasoning ping (spec 13): when an opus job lands, Brain publishes
            # brain.background_done with a short spoken summary → speak it. A dedicated event
            # (not brain.decision) so a background result is distinguishable from a live answer.
            self.bus.subscribe("brain.background_done", self._speak_background)

        # Live transcript tap + idle narration — only when explicitly enabled.
        if self._enable_session_awareness:
            self._start_session_awareness(speak)

        # Weekly graph refresh (spec 08 item 4) — only when explicitly enabled.
        if self._enable_graph_refresh:
            self._start_graph_refresh()

        # 5. start the Control API server on the unix socket.
        # VoiceIO pause/resume: mirror Control API transitions into voice.set_paused()
        # via the on_pause / on_resume callback seam (same pattern as on_kill).
        # Default lambda: None when no VoiceIO is wired → no-op, behaviour unchanged.
        _voice = self._voice
        self._control = ControlServer(
            socket_path=self.socket_path,
            state=self.state,
            budget=self._guard,
            on_kill=self._on_kill,
            on_pause=(lambda: _voice.set_paused(True)) if _voice is not None else (lambda: None),
            on_resume=(lambda: _voice.set_paused(False)) if _voice is not None else (lambda: None),
        )
        await self._control.start()

        # Live audio loop (spec 10): only with a real VoiceIO AND enable_voice, so
        # unit tests never open a mic. Runs the blocking mic/wake/STT loop in a worker
        # thread; each utterance publishes voice.utterance → Brain answers → speak.
        if self._voice is not None and self._enable_voice:
            self._start_voice_loop(self._voice)

        # 6. RUNNING.
        self.state.last_event = "daemon.started"

    async def _speak_decision(self, event: Event) -> None:
        """Speak the plain-answer ``brain.decision`` via VoiceIO (spec 10 §decision 3).

        Uses speak_response, not raw speak — a plain answer is a reply to the owner's
        utterance and must re-arm follow-up like any skill's reply (spec 03 §Follow-up).
        """
        if self._voice is None:
            return
        answer = str(event.payload.get("answer", ""))
        if answer:
            await self._voice.speak_response(answer)

    async def _speak_background(self, event: Event) -> None:
        """Speak a finished background-reasoning summary (spec 13 §on_done).

        Uses raw speak, NOT speak_response: this fires spontaneously whenever the
        background job happens to finish, not as a reply to something the owner just
        said — arming follow-up here would open a 10s wake-free ambient window at an
        unpredictable moment. Fail closed (spec 03 §Follow-up).
        """
        if self._voice is None:
            return
        answer = str(event.payload.get("answer", ""))
        if answer:
            await self._voice.speak(answer)

    def _start_voice_loop(self, voice: VoiceIOLike) -> None:
        """Start the blocking live mic loop as a background task (spec 10).

        ``run_live_loop`` is imported locally — it pulls the audio stack
        (sounddevice/openWakeWord/faster-whisper) which the daemon must not require
        on the voice=None path. NOTE: the loop runs via ``asyncio.to_thread``;
        cancelling this task does NOT stop the thread (``RawInputStream.read`` runs
        until process exit) — clean teardown is process exit, per the Completion Record.
        """
        # optional dep — voice path only
        from edith.voice.io import VoiceIO
        from edith.voice.live import resolve_wake_model, run_live_loop, wake_phrase

        # Resolve the wake model HERE, exactly as edith.voice.__main__ does. Calling
        # run_live_loop(voice) bare takes its _WAKE_MODEL default ("hey_jarvis"), which
        # silently ignored EDITH_WAKE_MODEL — so the daemon listened for "Hey Jarvis"
        # while the owner said "Hey Edith" and the trained hey_edith.onnx was never
        # loaded. Wake scored ~0.00 forever with no error anywhere.
        wake_model = resolve_wake_model()
        wake_threshold = float(os.environ.get("EDITH_WAKE_THRESHOLD", "0.5"))
        followup = float(os.environ.get("EDITH_FOLLOWUP_SECONDS", "10.0"))
        print(f"[edithd] wake model: {wake_model}  (threshold {wake_threshold}) — "
              f"say '{wake_phrase(wake_model)}, …'", flush=True)

        loop = asyncio.get_running_loop()
        self._voice_stop = threading.Event()
        self._voice_task = loop.create_task(
            run_live_loop(
                cast(VoiceIO, voice),
                wake_model=wake_model,
                wake_threshold=wake_threshold,
                followup_seconds=followup,
                stop=self._voice_stop,
            )
        )

    def _make_default_resolver(self, store: MemoryStore) -> ResolveRepoLike:
        """A ``resolve_repo``-shaped closure bound to this daemon's store+router.

        Kept as a closure (not ``functools.partial``) so the ``(name) -> …``
        signature the Brain expects is explicit and type-checks cleanly.
        """
        router = self._router

        async def resolve(name: str) -> ResolveResult:
            return await _resolve_repo_impl(name, store=store, router=router)

        return resolve

    def _start_session_awareness(self, speak: object) -> None:
        """Spin up the live transcript collector + idle-narration loop (spec 04).

        The Narrator speaks meaningful transitions; with no VoiceIO wired it narrates
        to a silent seam (events still update the Control API last_event via SessionBus).
        Both run as background tasks cancelled on shutdown.
        """
        assert self._session_bus is not None

        async def _silent(_text: str) -> None:
            return None

        # Narrator's budget_gate is zero-arg, so bind the tier it actually calls at:
        # _narrate_error uses Tier.HAIKU. On exhaustion the gate returns False and the
        # Narrator takes its SPOKEN-LOCAL template branch — she still speaks, just
        # without a model call. Narration degrades; it does not go silent.
        guard = self._guard
        narrator = Narrator(
            self.bus,
            speak if callable(speak) else _silent,  # type: ignore[arg-type]
            router=self._router,
            budget_gate=lambda: guard.budget_check(Tier.HAIKU),
        )
        collector = TranscriptCollector(self._session_bus.ingest)
        loop = asyncio.get_running_loop()
        self._session_tasks = [
            loop.create_task(collector.run()),
            loop.create_task(self._idle_loop(narrator)),
        ]

    @staticmethod
    async def _idle_loop(narrator: Narrator, interval: float = 30.0) -> None:
        """Drive the Narrator's timer-based idle sweep (spec 04 §Step 3)."""
        while True:
            await asyncio.sleep(interval)
            await narrator.tick()

    def _start_graph_refresh(self) -> None:
        """Start the weekly model-free graph refresh as a background task (spec 08 item 4).

        Uses the injected ``graph_refresh_fn`` if given (tests); otherwise, when Memory is a
        concrete ``MemoryStore``, builds the real default bound to THAT handle (mirrors
        ``_make_default_resolver``) — never a second Kuzu connection. With neither (a fake
        Memory in a test that also didn't inject a refresh_fn), there is nothing to run, so
        this is a no-op — ``enable_graph_refresh`` stays opt-in and never breaks such a test.
        """
        refresh_fn = self._graph_refresh_fn
        if refresh_fn is None:
            if not isinstance(self._memory, MemoryStore):
                return
            refresh_fn = self._make_default_graph_refresh(self._memory)
        loop = asyncio.get_running_loop()
        self._graph_refresh_task = loop.create_task(
            self._graph_refresh_loop(refresh_fn, self._graph_refresh_interval)
        )

    @staticmethod
    def _make_default_graph_refresh(store: MemoryStore) -> Callable[[], None]:
        """The real weekly pass: both orgs' metadata graph + a local reembed backfill.

        Model-free — ``ingest_workspace`` is the GitHub-API metadata pass (no clones, no
        model calls) and ``backfill_embeddings`` is the local sqlite-vec embedder (no
        Bifrost). Deep extraction (Opus per repo, ~2600 calls) is explicitly OUT OF SCOPE —
        see the Completion Record. Runs through the injected-store seam so this never opens
        a second Kuzu connection alongside the daemon's own.
        """

        def refresh() -> None:
            for org in _GRAPH_REFRESH_ORGS:
                ingest_workspace(org, store=store)
            backfill = getattr(store, "backfill_embeddings", None)
            if callable(backfill):
                backfill()

        return refresh

    def _wrap_graph_refresh_for_thread(self, refresh_fn: Callable[[], None]) -> Callable[[], bool]:
        """Wrap ``refresh_fn`` to run entirely inside the worker thread it's given to.

        Both the error containment AND the ``_graph_refresh_thread_idle`` completion signal
        live HERE, inside the thread body — not in the coroutine's ``finally`` — so they are
        accurate even if the awaiting task is cancelled out from under an already-running
        thread (``stop()`` waits on this event; see its docstring). Returns True on success,
        False on a declared, expected failure (mirrors ``BackgroundReasoner._run``'s declared
        MODEL_CALL_ERRORS catch — not a bare except; anything outside ``_GRAPH_REFRESH_ERRORS``
        is a real bug and is allowed to surface via "Task exception was never retrieved").
        """

        def run() -> bool:
            try:
                refresh_fn()
                return True
            except _GRAPH_REFRESH_ERRORS:
                return False
            finally:
                self._graph_refresh_thread_idle.set()

        return run

    async def _graph_refresh_loop(
        self, refresh_fn: Callable[[], None], interval: float
    ) -> None:
        """Refresh, then sleep ``interval``, forever (spec 08 item 4); cancelled in ``stop()``.

        Refresh-FIRST (not sleep-first): under launchd ``KeepAlive`` the daemon can restart far
        more often than weekly, and a sleep-first loop would reset the wait on every restart —
        plausibly never firing again once the graph goes stale (the whole premise of this item).
        Every write here is an idempotent MERGE-upsert (``store.py``'s ``_upsert_node``), so
        re-running on a fast restart loop is wasted work, not a correctness problem; a persisted
        last-refresh timestamp to avoid that waste is a separate, out-of-scope PR.

        The refresh runs in a worker thread via ``asyncio.to_thread`` (precedent:
        ``_start_voice_loop`` / ``voice/live.py``) so the ~1.3-min write pass never blocks the
        event loop. While it is in flight, ``_graph_refresh_in_progress`` is True, which — via
        the ``is_paused`` predicate wired into Brain in ``start()`` — makes Brain skip any
        turn's recall/remember for that window (no reply at all, not even an apology — the
        honest cost of this design; see the Completion Record) rather than run it concurrently
        against the same Kuzu ``Connection`` from a second thread. Setting/clearing that flag
        and checking ``is_paused``/``is_stopping`` all happen on this single event-loop thread
        (cooperative asyncio), so there is no race in the flag itself — only the window it
        protects. ``self.state.last_event`` is updated around the pass so Control API ``status``
        shows it actually ran (otherwise there would be no observable signal at all).

        NOTE — a narrower residual gap, stated rather than hidden: this gates Brain's live-turn
        pass only. ``BackgroundReasoner``'s ``on_done`` callback (``brain/loop.py``) and
        ``finder/resolve.py``'s fire-and-forget ``_deep_extract`` both call ``store.remember``
        from the loop thread WITHOUT checking ``is_paused`` at all — they could theoretically
        still race a refresh's worker thread. Gating those two is a separate cross-cutting
        change (they're both async-task callbacks landing at an arbitrary later time, not a
        single call site); out of scope here, flagged for a follow-up.
        """
        wrapped = self._wrap_graph_refresh_for_thread(refresh_fn)
        while True:
            if self.state.is_paused or self.state.is_stopping:
                await asyncio.sleep(interval)
                continue  # respect pause: skip this cycle, retry after the next interval
            self._graph_refresh_in_progress = True
            self._graph_refresh_thread_idle.clear()
            self.state.last_event = "graph_refresh.started"
            try:
                ok = await asyncio.to_thread(wrapped)
                self.state.last_event = "graph_refresh.done" if ok else "graph_refresh.failed"
            finally:
                self._graph_refresh_in_progress = False
            await asyncio.sleep(interval)

    def _on_kill(self) -> None:
        """Control API ``kill`` handler: schedule graceful shutdown.

        Runs inside the request handler, so it must not block on stopping the
        server it is being served by — schedule ``stop`` as a task and let the
        current response flush first.
        """
        asyncio.get_running_loop().create_task(self.stop())

    async def stop(self) -> None:
        """Graceful shutdown (spec §Shutdown): compact, close Memory, close socket."""
        if self._stopped.is_set():
            return
        # 1. stop accepting new intents: STOPPING makes Brain skip any late utterance.
        if self.state.state is not self.state.state.STOPPING:
            self.state.kill()

        # 1b. cancel session-awareness background tasks (collector tail + idle loop).
        for task in self._session_tasks:
            task.cancel()
        self._session_tasks = []
        # Stop the live-voice loop COOPERATIVELY, then join it. asyncio.to_thread cannot
        # interrupt a worker and cancelling the task around it does not stop the thread, so
        # this used to leave the mic thread running past shutdown with an open PortAudio
        # stream — the interpreter then tore down underneath it and Ctrl-C ended in a
        # segfault. Setting the flag lets the loop exit its RawInputStream context and close
        # the device first. It checks once per ~80 ms frame; the join is bounded so a wedged
        # audio read degrades to the old best-effort behaviour instead of hanging shutdown.
        if self._voice_task is not None:
            voice_task, self._voice_task = self._voice_task, None
            if self._voice_stop is not None:
                self._voice_stop.set()
            try:
                await asyncio.wait_for(voice_task, _VOICE_SHUTDOWN_JOIN_TIMEOUT)
            except (TimeoutError, asyncio.CancelledError):
                voice_task.cancel()
        # Cancel the graph-refresh loop (spec 08 item 4): stops it from starting another
        # cycle. This does NOT stop an already-running worker thread (asyncio.to_thread
        # can't interrupt one) — join it below, BEFORE compact()/close() touch the same
        # Memory handle the thread may still be writing to.
        if self._graph_refresh_task is not None:
            self._graph_refresh_task.cancel()
            self._graph_refresh_task = None
        # Bounded join on the real worker thread (set by ITSELF, not by the cancelled task
        # above — see _wrap_graph_refresh_for_thread). Runs off the loop via to_thread so a
        # slow-to-finish refresh doesn't freeze the event loop while shutdown waits on it.
        if not self._graph_refresh_thread_idle.is_set():
            await asyncio.to_thread(
                self._graph_refresh_thread_idle.wait, _GRAPH_REFRESH_SHUTDOWN_JOIN_TIMEOUT
            )
        # Cancel any in-flight background opus jobs (spec 13 §Shutdown ownership) — a job can
        # outlive the turn that started it; don't leave an opus call dangling past shutdown.
        if self._reasoner is not None:
            self._reasoner.cancel_all()

        # 2. final compact() — deferred on the real MemoryStore, so call it only
        #    if present (# TODO(compact): remove the guard once Memory.compact lands).
        compact = getattr(self._memory, "compact", None)
        if callable(compact):
            compact()

        # 3. close Memory (flush + release the Kuzu lock) if it exposes close().
        close = getattr(self._memory, "close", None)
        if callable(close):
            close()

        # 4. close the SecureStore (encrypted-volume impl unmounts here).
        self._store.close()

        # 5. close the Control API socket.
        if self._control is not None:
            await self._control.stop()
            self._control = None

        self._stopped.set()

    async def wait_stopped(self) -> None:
        """Await graceful shutdown completing (used after a Control API ``kill``)."""
        await self._stopped.wait()
