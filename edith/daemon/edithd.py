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
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import keyring
from keyring.errors import KeyringError

from edith.brain import Brain
from edith.brain.loop import MemoryLike, ResolveRepoLike, RouterLike
from edith.bus import EventBus
from edith.daemon.control import BudgetView, ControlServer
from edith.daemon.securestore import LocalSecureStore, SecureStore
from edith.daemon.state import RuntimeState
from edith.finder import ResolveResult
from edith.finder import resolve_repo as _resolve_repo_impl
from edith.memory.store import MemoryStore
from edith.skills import PRReviewSkill

_KEYRING_SERVICE = "edithd"
_SOCKET_NAME = "edithd.sock"


class VoiceIOLike(Protocol):
    """The slice of VoiceIO that edithd uses (spec 03 §Wiring).

    Mirrors the MemoryLike / RouterLike pattern: edithd depends on this
    interface, not the concrete VoiceIO class, so tests can pass fakes without
    subclassing. A real ``edith.voice.io.VoiceIO`` satisfies this structurally.
    """

    async def speak(self, text: str) -> None: ...

    def set_paused(self, paused: bool) -> None: ...


@dataclass(frozen=True)
class Secrets:
    """The secrets edithd holds in RAM only (never logged, never persisted)."""

    bifrost_api_key: str
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


class _ZeroBudget:
    """Budget seam until Guard lands (Control API ``budget_used`` -> 0)."""

    def budget_used(self) -> int:
        # TODO(Guard): Guard owns the real per-window budget counter.
        return 0


class EdithDaemon:
    """Composes the subsystems and runs the daemon lifecycle."""

    def __init__(
        self,
        data_dir: str | Path,
        secrets: Secrets,
        memory: MemoryLike,
        router: RouterLike,
        secure_store: SecureStore | None = None,
        budget: BudgetView | None = None,
        resolve_repo: ResolveRepoLike | None = None,
        voice: VoiceIOLike | None = None,
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
        self._store: SecureStore = secure_store or LocalSecureStore(data_dir)
        self._budget: BudgetView = budget or _ZeroBudget()
        self.state = RuntimeState()
        self.bus = EventBus()
        self._brain: Brain | None = None
        self._control: ControlServer | None = None
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

        # Register skills so a voice.utterance can dispatch them (spec 02
        # build-step 3). When VoiceIO is wired, pass its speak seam into
        # PRReviewSkill so review findings are spoken aloud. Without VoiceIO
        # the default _silent seam keeps the confirm gate safely denied.
        pr_skill = (
            PRReviewSkill(self._router, speak=self._voice.speak)
            if self._voice is not None
            else PRReviewSkill(self._router)
        )
        self._brain = Brain(
            bus=self.bus,
            memory=self._memory,
            router=self._router,
            is_paused=lambda: self.state.is_paused,
            resolve_repo=resolver,
            skills=[pr_skill],
        )

        # 5. start the Control API server on the unix socket.
        # VoiceIO pause/resume: mirror Control API transitions into voice.set_paused()
        # via the on_pause / on_resume callback seam (same pattern as on_kill).
        # Default lambda: None when no VoiceIO is wired → no-op, behaviour unchanged.
        _voice = self._voice
        self._control = ControlServer(
            socket_path=self.socket_path,
            state=self.state,
            budget=self._budget,
            on_kill=self._on_kill,
            on_pause=(lambda: _voice.set_paused(True)) if _voice is not None else (lambda: None),
            on_resume=(lambda: _voice.set_paused(False)) if _voice is not None else (lambda: None),
        )
        await self._control.start()

        # 6. RUNNING.
        self.state.last_event = "daemon.started"

    def _make_default_resolver(self, store: MemoryStore) -> ResolveRepoLike:
        """A ``resolve_repo``-shaped closure bound to this daemon's store+router.

        Kept as a closure (not ``functools.partial``) so the ``(name) -> …``
        signature the Brain expects is explicit and type-checks cleanly.
        """
        router = self._router

        async def resolve(name: str) -> ResolveResult:
            return await _resolve_repo_impl(name, store=store, router=router)

        return resolve

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
