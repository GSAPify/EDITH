# 10 — Daemon composition root (full voice → graph)

> Closes the long-standing daemon-integration gap: `EdithDaemon` is only ever built in
> tests, `voice=None` on every real path, and nothing speaks the plain-answer path. This
> slice makes `python -m edith.daemon` boot the full daemon with a **real VoiceIO**, so the
> **graph-backed Brain** (semantic recall + cross-session `remember()` + all skills) is what
> answers voice — not the standalone `edith.voice.__main__` harness's direct model call.

## Purpose

Today two halves don't meet:
- `python -m edith.voice` (standalone harness) has the live mic/wake/STT loop + an in-session
  `TurnBuffer`, but calls `router.model_call` DIRECTLY — no Memory, no skills, no Brain.
- `edithd` builds the full `Brain` (Memory + Router + skills + resolve-on-miss + `history` seam +
  session awareness) but `voice=None`, so the daemon never speaks or listens.

This slice bridges them: the daemon constructs a real `VoiceIO`, runs the live loop, routes
`voice.utterance` through its `Brain`, and speaks the answer. EDITH then answers with the real
graph + cross-session memory, and every skill (desktop control, PR review, session query) becomes
voice-reachable through one process that owns the Kuzu handle.

## Design decisions (the four that gated this)

1. **Persona into Brain (injectable preamble).** Brain uses a generic `_SYSTEM_PREAMBLE`. The
   voice path needs the JARVIS "sir"/brevity persona. Add `system_preamble: str | None = None`
   to `Brain.__init__` (default → current constant, so existing behavior is unchanged). The
   daemon passes the voice persona. Extract the persona to a shared `edith/voice/persona.py` so
   the harness and the daemon can't drift.

2. **Brevity.** Add `answer_max_tokens: int | None = None` to `Brain.__init__`; when set, Brain
   passes it to the answer `model_call`. The daemon passes 120 (voice latency discipline). Default
   None → Router default, unchanged for non-voice callers.

3. **Speak-the-decision.** Brain publishes `brain.decision` ONLY on the plain-answer path (a skill
   that handles a turn publishes `skill.result` and returns early, and skills speak themselves via
   their injected `speak`). So a daemon subscriber on `brain.decision` → `voice.speak(answer)`
   speaks the plain-answer path with NO double-speak. Wired only when voice is present.

4. **Enable flag + Kuzu single-owner.** The live audio loop starts as a background task ONLY when
   voice is present AND `enable_voice=True` (mirrors `enable_session_awareness`), so tests never
   open a mic. The daemon builds the ONE `MemoryStore` and owns the Kuzu handle for its lifetime —
   the viewer/finder/ingest must not run against `memory.kuzu` while the daemon is up (documented
   constraint; prod fix = route those through the daemon, out of scope here).

## Changes

- **`edith/brain/loop.py`** — `Brain.__init__` gains `system_preamble: str | None = None` and
  `answer_max_tokens: int | None = None`. `_assemble` takes the preamble; the answer `model_call`
  passes `max_tokens` when set. `history` already exists (conversation-mode). All defaults preserve
  current behavior → existing tests stay green.
  - **Model-error seam (decision #5, from review).** The standalone harness caught
    `(TimeoutError, httpx.HTTPError)` around its `model_call` and spoke an apology; in the daemon
    the call lives INSIDE `Brain._on_utterance` with no guard, so a network blip would go silent
    (worse than the harness) — and the speak-the-decision subscriber can't help because on failure
    `brain.decision` is never published. Fix: Brain catches the router's declared transport errors
    around the answer `model_call` and publishes a `brain.decision` carrying a graceful fallback
    answer ("Sorry sir, I couldn't reach the model just now."), which the subscriber speaks
    unchanged. To avoid coupling Brain to `httpx`, the router package exports the tuple
    `MODEL_CALL_ERRORS = (TimeoutError, httpx.HTTPError)`; Brain catches that (specific, not a bare
    except). Headless-testable: a fake router that raises → assert the decision carries the apology.
- **`edith/voice/persona.py`** (new) — the shared `VOICE_PERSONA` constant (moved from
  `edith/voice/__main__.py`, which imports it).
- **`edith/daemon/edithd.py`** — `EdithDaemon` gains `enable_voice: bool = False`. When
  `voice is not None`: build `Brain(system_preamble=VOICE_PERSONA, answer_max_tokens=120,
  history=TurnBuffer())`; subscribe `brain.decision` → `voice.speak`. When also `enable_voice`:
  start `run_live_loop(voice, …)` as a background task (cancelled on shutdown, like the session
  tasks).
- **`edith/daemon/__main__.py`** (new) — `python -m edith.daemon`: `resolve_secrets()` → build
  real `MemoryStore(~/.edith/data/memory.kuzu)` + `Router` + `build_live_voice_io(bus, engine=…)`
  → `EdithDaemon(…, voice=voice, enable_voice=True)` → `start()` → run until `kill`/Ctrl-C →
  `stop()`. Engine + wake model via the existing env knobs.

## Verification

- **Headless (unit):** Brain honors an injected `system_preamble` and `answer_max_tokens` (the
  answer `model_call` sees the persona + the cap); a multi-turn test still shows history splicing;
  Brain catches a raising router and publishes a fallback `brain.decision` (the error seam); the
  `brain.decision` → `speak` subscriber speaks the plain-answer path and does NOT fire on a skill
  turn (no double-speak); `EdithDaemon(enable_voice=False)` starts without touching audio (existing
  daemon tests stay green). **`__main__` is NOT unit-tested** — it opens the real single-owner
  `memory.kuzu`, so a fake-wired construction test buys ceremony, not safety; keep it thin and
  owner-smoke it like `edith.voice.__main__`.
- **Owner LIVE-SMOKE (mic/speaker/graph):** `python -m edith.daemon --engine elevenlabs` → "Hey
  Edith, what do you know about the concorde_lib repo?" → answer draws on the **real graph** (not
  just the session); a fact taught in one run is recalled in the next (cross-session `remember`);
  "open Spotify" / "start OMC in <repo>" fire the desktop skill through the daemon.

## Deferred / risks

- The standalone `edith.voice.__main__` harness stays as a lighter, graph-free smoke tool (no
  Kuzu) — the daemon is the real path. Persona shared via `persona.py` so they don't drift.
- **Mic shutdown is NOT clean (be honest).** `run_live_loop` runs `_blocking_listen` via
  `asyncio.to_thread`; cancelling that task does NOT kill the thread — `RawInputStream.read()`
  runs until process exit. Fine for an owner-run Ctrl-C smoke, but the completion record must say
  so rather than implying the live-loop task cancels cleanly like the session tasks.
- Guard (authorize/budget) still the allow-by-default seam; the daemon's `_ZeroBudget` stands.
- Kuzu single-owner (above). `Memory.compact()` still deferred (the shutdown path calls it
  defensively if present) — its own follow-up.

---

## Completion Record — 2026-07-17 (Session 18)

- **What shipped:** `python -m edith.daemon` now boots the full daemon with a real `VoiceIO` on
  a shared bus, so the graph-backed Brain answers voice with semantic recall + cross-session
  memory + all skills. Closes the daemon-integration gap (Slice 1's `voice=None`-everywhere).
- **How it works:** entry point builds `VectorMemoryStore(~/.edith/data/memory.kuzu)` + `Router` +
  `build_live_voice_io(bus)` → `EdithDaemon(bus=bus, voice=voice, enable_voice=True)`. On the
  voiced path the daemon gives Brain `system_preamble=VOICE_PERSONA`, `answer_max_tokens=120`, and
  a `TurnBuffer`; subscribes `brain.decision` → `voice.speak` (plain-answer path only — no
  double-speak, verified); and runs `run_live_loop` as a background task (gated by `enable_voice`).
- **Key decisions:** (1) injectable `system_preamble`/`answer_max_tokens` on Brain, defaults
  preserve all existing behavior; (2) shared `edith/voice/persona.py` so harness + daemon don't
  drift; (3) **model-error seam** — Brain catches `MODEL_CALL_ERRORS` (exported by the router) and
  publishes a graceful fallback `brain.decision`, so a Bifrost blip speaks an apology instead of
  going silent (the daemon has no other handler on this path); (4) injected `bus` so the VoiceIO
  publishes onto the bus Brain reads.
- **Deviations:** tightened BOTH `MemoryLike` protocols' `remember(edges=...)` from `list[object]`
  to `list[Edge]` (the real store's type) — needed for a real `VectorMemoryStore` to satisfy the
  daemon's `MemoryLike`; removed the now-unnecessary `list[object]` annotation in `pr_review.py`.
  Correctness cleanup, not scope creep.
- **Verification:** 302 passed, 2 skipped; ruff + `pyright edith` clean. Headless-tested: Brain
  persona/max_tokens/error-seam, speak-the-decision + no-double-speak, `enable_voice=False` starts
  without audio, `__main__ --help`. **`__main__` and the live loop are owner LIVE-SMOKE only.**
- **Known gaps / honest notes:** mic shutdown is NOT clean — the `asyncio.to_thread` mic loop runs
  until process exit (Ctrl-C), the task cancel is best-effort. Kuzu single-owner (viewer/finder/
  ingest must be closed while the daemon runs). Guard + `Memory.compact()` still deferred seams.
- **Owner LIVE-SMOKE (pending):** `python -m edith.daemon --engine elevenlabs` → ask about a repo
  in the graph → answer draws on the real graph; teach a fact, restart, confirm it's recalled
  (cross-session `remember`); "open Spotify" fires the desktop skill through the daemon.
