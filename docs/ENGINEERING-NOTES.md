# Engineering notes

The sharp edges. Everything here was verified against the code, with line references where they
help. The README states what is true; this file states *how narrowly* it is true, which is the part
that matters when you are about to rely on something.

Nothing in here is aspirational. If a claim could not be verified without hardware, it says so.

## Contents

- [Guard: what the autonomy gate actually reaches](#guard-what-the-autonomy-gate-actually-reaches)
- [The token budget, and why EDITH cannot go mute](#the-token-budget-and-why-edith-cannot-go-mute)
- [Redaction: one choke-point, and where it does not run](#redaction-one-choke-point-and-where-it-does-not-run)
- [The Kuzu lock](#the-kuzu-lock)
- [Graph refresh semantics](#graph-refresh-semantics)
- [The half-duplex mic gate, and the duplex spike](#the-half-duplex-mic-gate-and-the-duplex-spike)
- [CoreAudio/PortAudio resilience: bounded reopen, not silence](#coreaudioportaudio-resilience-bounded-reopen-not-silence)
- [Baseline quirks on a clean checkout](#baseline-quirks-on-a-clean-checkout)
- [Owner live-smoke ledger](#owner-live-smoke-ledger)
- [Verification convention](#verification-convention)

## Guard: what the autonomy gate actually reaches

`edith/daemon/__main__.py` builds the daemon's single `Guard` with an **allowlist** of the closed
desktop vocabulary:

```python
Guard(allowlist=frozenset(intent.value for intent in Intent))
```

`Intent` has exactly four members: `open_app`, `spotify`, `terminal`, `omc_launch`. The allowlist
flips the posture from "allow unless denylisted" to **deny unless vetted**, which is the correct
shape for a finite enum: a denylist can only ever cover verbs somebody thought to add to it, and a
future `Intent` added without a deliberate Guard decision now fails closed.

Precedence inside `authorize`, in order: a denylisted action is DENY even if it also asks for
confirmation; an allowlist, when set, then denies anything not on it; `needs_confirmation` then
yields ASK; otherwise ALLOW. Membership is exact on a normalized action verb, never a substring of a
raw shell string.

Three qualifications matter more than the headline:

1. **Guard matches the intent verb, never the argument.** `"open Slack"` and `"open /tmp/evil.app"`
   are both `open_app`. The attacker-planted-bundle case is closed by the executor refusing bundle
   *paths*, not by the gate, and no denylist or allowlist setting could close it. Argument safety
   lives with the executors, permanently.
2. **ASK is unreachable.** `DesktopControlSkill.needs_confirmation` is a `False` class constant, and
   `PRReviewSkill`'s `Confirm` callable defaults to `_deny` with the daemon wiring that default. The
   middle verdict therefore cannot occur anywhere in the shipped daemon. What would make ASK mean
   something is a real voice-confirm ("should I?", then listen), which does not exist yet.
3. **`EdithDaemon` falls back to a bare `Guard()`** when no Guard is injected, which is denylist-only
   and so effectively inert. That is the test and embedding path. The shipped
   `python -m edith.daemon` always injects the allowlisted one, but do not read code that constructs
   `EdithDaemon` directly as being gated.

History worth keeping: the gate was inert in every shipped config until the allowlist landed. The
hole was found empirically rather than by inspection, by noticing that `Intent` and the default
denylist (`rm_rf`, `drop_table`, `force_push`, `shutdown`, `disk_wipe`) have an **empty
intersection**, which made `authorize()` provably ALLOW for 100% of desktop actions.

## The token budget, and why EDITH cannot go mute

One `Guard` per daemon process, constructed before the Router so the ordering is explicit. It is
handed to the Router (`budget_check` before a call, `record` after), the background reasoner, the
Narrator's budget gate, and the Control API's budget view so the menu bar shows real usage.

The charge path is the part that would silently break: an `on_usage` seam on `Router` calls
`Guard.record` from both `model_call` and `model_call_stream`. `model_call_masked` delegates to both,
so it is charged twice for its two genuine billing events and **must not** gain a third call site.

The safety property, verified rather than intended: `budget_check` is only ever called with
`Tier.OPUS`, plus `Tier.HAIKU` for narration. Nothing gates the live conversational path. Budget
exhaustion therefore downgrades opus to Sonnet and drops narration to a local template, and EDITH
keeps talking.

## Redaction: one choke-point, and where it does not run

`edith/memory/secrets.py::sanitize_text` runs inside every `model_call*` and on every TTS or bus
payload. It covers assignments, provider tokens, PEM blocks, and `scheme://user:PASSWORD@host`
connection URIs. That last pattern was added because it was the real leak in practice: pasted
Snowflake and Postgres credentials flowing through a session transcript.

Where it does not run: **log records**. Python's `lastResort` handler sends WARNING and above to
stderr, and the LaunchAgent captures that permanently to `~/.edith/logs/edithd.err.log`. Poll errors
derived from tailing `~/.claude/projects` are the realistic source. Keep `~/.edith` at mode 700, and
add a `newsyslog.d` entry if unbounded growth matters. `--show-transcript` writes to the same
unredacted, unrotated destination under launchd, which is why it is off by default and why the
README says to use it in a terminal instead.

## The Kuzu lock

Kuzu embedded is single-process and single-writer. `edithd` opens `MemoryStore` once at startup and
holds it for the life of the process, so the viewer, finder and ingest cannot open
`~/.edith/data/memory.kuzu` while the daemon runs.

Verified from the code, because the intuitive answers are all wrong:

- **`pause` does not release the handle.** `RuntimeState.pause()` flips an enum and never touches
  `self._memory`. It is a behavior switch, not a resource-release one.
- **The handle is released only by `MemoryStore.close()`**, which is called only by
  `EdithDaemon.stop()`, which runs only on a graceful shutdown: a Control API `kill`, or Ctrl-C.
- **Under launchd `KeepAlive`, `kill` is immediately undone.** The process exits cleanly and launchd
  respawns it within moments, which reopens the graph and retakes the lock. `kill` does not leave a
  window you can rely on.
- **`launchctl bootout` is the only way to actually free it**, at the cost of an ungraceful stop:
  `bootout` delivers `SIGTERM`, the daemon installs no signal handler, so `compact()` and `close()`
  do not run. Whatever lock release follows is a property of Kuzu's own file locking, which is a
  third-party C++ library and not something this repo verified.

The production fix is routing all database access through `edithd`, so one process owns the handle
and every other surface talks over the Control API. That is noted, not built.
[`deploy/README.md`](../deploy/README.md) carries the line-cited version of this and the install
runbook.

## Graph refresh semantics

`--graph-refresh` runs the model-free passes in-process: both orgs' metadata pass plus a local
reembed. It never deep-extracts, because deep extract is an opus call per repo and scheduling that
across the whole workspace is not something a background timer should decide.

It refreshes on start and then every 7 days of **uptime**. That is an interval, not a calendar
schedule, and there is no persisted last-run timestamp, so a machine that reboots daily gets a daily
pass rather than a weekly one. Writes are idempotent upserts, so the cost of that is wasted work
rather than corruption.

During a refresh EDITH ignores turns. The refresh reuses the daemon's Kuzu handle from a worker
thread, and rather than take a cross-thread lock it folds into the existing `is_paused` predicate, so
a live turn is skipped entirely for roughly 1.3 minutes: no reply, not even an apology. That was a
deliberate trade, avoiding a class of deadlock for a once-a-week event. Two writers still bypass the
flag: `BackgroundReasoner.on_done` and `finder/resolve.py`'s deep-extract both write that handle
without checking `is_paused`, so they can in principle race the refresh thread.

Cost note, since an earlier version of this claim was wrong: the workspace metadata pass is one
paginated `gh api` call per org, roughly 13 requests for a 1300-repo org against a 5000/hour limit.
Rate limiting is a non-issue. The real cost is the serial Kuzu writes, benchmarked at about 54 ms per
repo. The `last_commit_date` incremental skip is therefore a lock-duration optimization rather than a
cost control, and it is not built for the metadata pass.

## The half-duplex mic gate, and the duplex spike

The shipped mic gate is half-duplex: wake detection is skipped entirely while EDITH speaks, plus a
2.5 second cooldown afterwards. Session narration has no cooldown of its own, so with several active
Claude Code sessions she can talk almost continuously and never hear the wake word. That is what
`--no-session-narration` works around. The flag disables narration only; the gate is unchanged, so
wake still pauses while she speaks for any other reason.

`edith/voice/duplex/` and `edith/voice/aec_bench/` exist to fix the cause rather than the symptom.
Where the spike actually stands:

- **`VpioDuplex`** wraps macOS Voice Processing I/O, which is Apple's own canceller, reached through
  AVAudioEngine and the PyObjC AVFoundation bindings. Measured **7.8 dB ERLE** on this MacBook.
- **The SpeexDSP arm is a canceller core only.** There is no `DuplexAudio` backend around it yet, so
  `--backend speex` raises `DuplexUnavailable`. It binds `libspeexdsp` through ctypes and needs no
  Python package, just `brew install speexdsp`.
- **WebRTC AEC3 was the intended comparison and cannot be built here.** The only PyPI package passes
  32-bit ARM flags (`-mfloat-abi=hard -mfpu=neon`) that clang rejects on arm64, along with
  `-DWEBRTC_LINUX` on macOS, and there is no homebrew formula. A true AEC3 arm means vendoring and
  patching that build.
- **`added_latency_ms` is knowingly wrong.** It reports playback queue depth, measured at 1664 ms on
  a run whose true added latency is single-digit milliseconds. The metric function itself is correct
  and unit-tested against known shifts; the capture loop's pacing is not sample-accurate enough to
  feed it. Fixing that means driving playback and capture off one clock instead of interleaving two
  loosely-coupled loops.

Three measurement traps the bench encodes, each of which produced a wrong number first:

1. **A muted-speaker guard cannot look at the cancelled capture.** A working canceller removes
   exactly the echo energy such a guard searches for, so it cannot distinguish a dead speaker from a
   canceller doing its job. The silent-mic floor belongs on the AEC-off pass.
2. **ERLE needs the same acoustic path captured twice**, once with cancellation off and once on.
   Comparing the digital stimulus against the capture measures speaker-to-air-to-mic path loss, not
   cancellation. `VpioDuplex` takes `enable_aec` for exactly this reason.
3. **Double-talk needs a second physical device.** Anything played through the laptop speaker is
   already in the canceller's reference, so using it would measure "the echo was cancelled" and
   report a catastrophic score for a canceller working perfectly. Hence `--near-end` and a phone at a
   fixed position. Omitted, double-talk is reported as null rather than fabricated.

Nothing in the voice path consumes any of this yet.

## CoreAudio/PortAudio resilience: bounded reopen, not silence

Before this fix, a `PaMacCore (AUHAL) err=-50` (or any other `sounddevice.PortAudioError`
while opening or reading the input stream) killed the mic thread permanently: the daemon and
Control API kept reporting RUNNING, but the voice loop was gone, silently. A shared
Bluetooth headset used for both input and output (`ULT WEAR`, common on this box) is a
plausible trigger — a CoreAudio profile renegotiation on that one device can knock out the
open stream.

The fix is a bounded, generic retry seam, `edith.voice.live._run_recoverable`: opening or
reading raises `sd.PortAudioError` → the failed stream context is closed, wake/conversation/
preroll state is reset fail-closed, then a bounded wait (default **1.0 s**,
`EDITH_AUDIO_RETRY_SECONDS` overrides it) — interruptible via `threading.Event.wait`, so a
cooperative `stop()` during shutdown does not have to wait out the delay — and the stream is
genuinely reopened. It never converts a dead stream into endless zero-frames; it either reads
real audio or keeps retrying, and every retry logs a warning with the exception type/message
only (never audio or transcript content).

`resolve_audio_retry_seconds()` parses `EDITH_AUDIO_RETRY_SECONDS` with `float()`, which
accepts `"nan"`/`"inf"`/`"-inf"` without raising — those, plus any negative value (which would
make the wait meaningless or invert it) or a malformed string, all fall back to the 1.0 s
default via an explicit `math.isfinite()` + non-negative check. `0` is accepted (an immediate,
uninterrupted-retry loop), as is any other finite non-negative value.

Two related gaps closed alongside it:

- **The voice-loop task had no done callback.** `EdithDaemon._start_voice_loop` now attaches
  `_on_voice_task_done`: a cancelled or normally-completed task stays silent, but an
  unexpected exception is logged once and sets `RuntimeState.last_event = "voice.failed"`, so
  Control API `status` can reveal a degraded voice path while the daemon stays up. It does
  **not** respawn the loop — an arbitrary programming exception is a different failure class
  than the bounded PortAudio retry above, which already lives inside the audio loop itself.
- **`stop()` used to abort shutdown on an already-failed voice task.** Awaiting a done task
  re-raises its exception on every await, not just the first — so once `_on_voice_task_done`
  had already reported a failure, `EdithDaemon.stop()`'s own `asyncio.wait_for(voice_task, …)`
  join would re-raise that same exception and abort everything after it in `stop()`: Memory/
  Kuzu close, the reasoner, SecureStore, Control API teardown, and `_stopped.set()`. `stop()`
  now catches the declared `Exception` case (never `BaseException`, so a `CancelledError` — the
  voice task's own, or `stop()`'s own coroutine being cancelled — still propagates via the
  existing `TimeoutError`/`CancelledError` clause) and logs it at debug level without
  re-reporting, since the done callback already did that once.
- **TTS output streams leaked.** Both `ElevenLabsAdapter` and `PiperAdapter` opened a fresh
  `sd.RawOutputStream` per utterance and never closed it. `_RawOutputSink` wraps the stream in
  a closable callable; the adapters close it exactly once per utterance on every exit path
  (normal completion, a stream/adapter error, or `handle.stop()`/cancellation) via a plain
  `getattr(sink, "close", None)` check, so an injected plain-function sink (as most tests use)
  is untouched.

Device overrides: `EDITH_INPUT_DEVICE` (input) and `EDITH_OUTPUT_DEVICE` (output, both TTS
adapters) accept the same shape sounddevice does — unset/blank keeps the sounddevice default,
an ordinary signed integer string is a device index, anything else is a name/query string
(`edith.voice.adapters.resolve_device_override`). Selected device identifiers are logged only
when `EDITH_VOICE_DEBUG=1`, and neither default exposes a private device name. The parser calls
`int()` directly inside a `try`/`except ValueError`, rather than `str.isdigit()` guarding an
`int()` call: `isdigit()` is `True` for some Unicode numerics (e.g. a superscript digit) that
`int()` itself cannot parse, which used to raise instead of falling through to the query-string
branch like every other non-integer input (round 2 review).

This is a half-duplex-only fix. VPIO/Speex duplex wiring (see the section above) is explicitly
out of scope here — the retry seam only concerns keeping the existing half-duplex mic stream
alive across a transient CoreAudio failure.

### Round 2 review: a bounded ceiling, sticky health, and real barge-in cutoff

Four gaps in the fix above, closed together:

**1. The retry loop had no ceiling.** A permanently missing/bad input device retried
`_run_recoverable` forever: the voice task never exited, so its failure could never reach
`_on_voice_task_done`, and the daemon reported RUNNING indefinitely with a dead mic.
`EDITH_AUDIO_MAX_RETRIES` (default **5**, via `resolve_audio_max_retries()`) now bounds
consecutive failures; the ceiling failure is **re-raised**, not swallowed, so it propagates
through `_blocking_listen` to the voice task and `_on_voice_task_done` observes it normally.
The parser accepts only a finite integer `>= 1` — `int()` already rejects `"nan"`/`"inf"`/
non-integer strings by raising, so the only extra check needed is `value < 1` (malformed,
zero, or negative all fall back to the default rather than disabling the ceiling). Staying
operational for at least `_AUDIO_HEALTHY_SECONDS` resets the consecutive-failure counter (see
"Round 3 review" below for why a bare successful open is not enough on its own); an
intermittently-flaky stream that keeps running fine between glitches never hits the ceiling,
while a device that never opens — or opens but can never actually be read — escalates
deterministically within exactly `max_retries` attempts. `stop()` being set during the retry
wait still exits immediately with no raise, exactly as before the ceiling existed — the
ceiling only changes what happens when retries, not a cooperative stop, exhaust the budget.

**2. There was nowhere durable to see the failure.** `last_event` is a busy, general-purpose
label — session narration and the weekly graph refresh both overwrite it constantly — so using
it to mean "voice is down" meant the very next unrelated event would silently un-flag a real
failure. `RuntimeState.voice_health` (`edith.daemon.state.VoiceHealth`, `HEALTHY`/`FAILED`) is a
dedicated, sticky field instead: `_on_voice_task_done` calls `mark_voice_failed()` alongside its
existing `last_event = "voice.failed"` write, `_start_voice_loop` calls `mark_voice_healthy()` on
every fresh start (so a stale failure from a prior run can't linger), and nothing else in
`RuntimeState` touches it. Control API `status` now returns five keys —
`{state, active_skill, budget_used, last_event, voice_health}` — the addition is deliberately
additive; `edith.menubar.controller.format_label` only reads two of the original four keys via
`.get()`, so existing clients are unaffected.

**3. Cancellation drained instead of cutting off.** `_RawOutputSink.close()` (`stream.stop()`
then `stream.close()`) waits for queued audio to finish playing before releasing the stream —
correct for normal completion, wrong for a barge-in: the owner interrupting EDITH mid-sentence
would still hear the buffered tail play out. `_RawOutputSink` now also has `abort()`
(`stream.abort()` then `stream.close()`, PortAudio's "discard immediately" call), sharing the
same `_closed` guard so at most one of the two ever runs the real teardown. Both adapters'
background tasks (`ElevenLabsAdapter._run_stream`, `PiperAdapter._drain`) track a local
`aborting` flag, set only in a `except BaseException: aborting = True; raise` clause, and choose
`_teardown_sink(sink, aborting=...)` in a `finally` — cancellation and genuine stream/read errors
call `abort()`, normal completion calls `close()`. `_teardown_sink` swallows (logs at debug)
anything the teardown call itself raises, so a broken sink's `abort()`/`close()` can never
replace the original `CancelledError` or stream exception being propagated. `_abort_sink` falls
back to `close()` for an injected sink exposing only `close()` (or is a no-op for a plain
callable sink with neither) — no existing test double needs to change.

**4. `handle.stop()` was called from the wrong thread.** Barge-in can land while the live mic
loop's worker thread is between frames, and `VoiceIO._on_wake`/`is_speaking` call `handle.stop()`
synchronously from wherever they run — which, transitively, is the mic thread, not the event
loop that owns the TTS task. `asyncio.Task.cancel()` is not documented thread-safe.
`_ElevenLabsHandle`/`_PiperHandle` now capture the loop returned by `asyncio.get_running_loop()`
inside `speak()` and `stop()` schedules the actual `task.cancel()` (and, for Piper,
`proc.terminate()`) via `loop.call_soon_threadsafe(...)` instead of calling them directly. This
adds one extra event-loop tick before cancellation actually lands compared to the old direct
call — tests that assert post-`stop()` state now await the handle's task to completion (or an
extra `asyncio.sleep(0)`) rather than assuming a single tick, and this has no operational effect
since barge-in polling already happens once per ~80 ms mic frame.

### Round 3 review: the ceiling was still evadable, and a closed-loop stop() could raise

**1. A successful open is NOT healthy by itself.** Round 2's ceiling reset
`consecutive_failures` to zero the instant `open_stream()` returned — before `listen()` had
run at all. An open-succeeds/read-immediately-fails cycle therefore reset to zero on *every*
attempt and could never accumulate past one, evading the ceiling entirely for exactly the
device class the ceiling exists to catch: one that opens fine but can never actually be read.
`_run_recoverable` now takes an injectable monotonic clock (`now`, defaults to
`time.monotonic`) and a module-level `_AUDIO_HEALTHY_SECONDS = 1.0`. The open time is recorded
only after context ENTRY (i.e. once `open_stream()` has genuinely succeeded and the `with`
block's body is about to run), not merely after calling `open_stream()`. On the next failure —
whether from that same stream failing again or from a subsequent `listen()` — the counter
resets to a fresh streak (this failure becomes attempt 1) *only if* the stream stayed
operational for at least `_AUDIO_HEALTHY_SECONDS` since that open; an immediate failure
(elapsed `< _AUDIO_HEALTHY_SECONDS`) instead continues the existing streak, genuinely
accumulating toward `max_retries` and re-raising at exactly the ceiling — no extra
reset/wait/open beyond it, and `stop()` during the wait still wins with no raise.

**2. `handle.stop()` could raise from the mic thread during degraded shutdown.** Round 2 moved
`task.cancel()`/`proc.terminate()` onto the owning event loop via `loop.call_soon_threadsafe`
so cancellation from the mic thread was thread-safe — but `call_soon_threadsafe` itself raises
`RuntimeError` if the loop has already closed, which can happen if `stop()` lands late in a
degraded/last-gasp shutdown. `_ElevenLabsHandle.stop()` and `_PiperHandle.stop()` now route
through a shared `_schedule_threadsafe` helper: it no-ops if `loop.is_closed()` is already
true, and otherwise suppresses a `RuntimeError` raised by `call_soon_threadsafe` itself (the
loop closing in the race window between the check and the call) — there is nothing left to
cancel once the loop is gone, so this becomes a no-op rather than crashing the mic thread.

## Baseline quirks on a clean checkout

Three failures fire before you touch anything. None is caused by your change, and each is left alone
deliberately.

| What you see | Why | What to do |
|--------------|-----|------------|
| `ruff check .` reports 3 SIM115 | They are in `scripts/sagemaker/train_hey_edith.py`, a training script rather than library code | Scope lint to `ruff check edith tests`, and do not "fix" it inside an unrelated PR |
| pyright emits `reportMissingImports` for `httpx`, `keyring`, `rumps` | It is not resolving `.venv` | Ignore, or point pyright at the venv locally |
| `uv run pytest` fails to resolve | A locally installed Python 3.14 makes `edith[voice]`'s `onnxruntime<1.20` pin conflict with `fastembed`'s marker split for `python_full_version >= 3.14`. Reproduces on a bare checkout | Use `uv run --frozen`, or `.venv/bin/python -m pytest` |

One dependency pin is load-bearing and has a saga behind it: `onnxruntime<1.20`. Versions 1.20 and
above silently miscompute openWakeWord's 2022-era ONNX models on macOS arm64, collapsing every wake
score to approximately zero. On 1.18.1, "hey edith" detects at 0.91.

## Owner live-smoke ledger

These paths are seam-tested, not hardware-verified. Tests passing is not evidence for any of them,
and this project has been bitten by exactly that: the wake loop passed its full suite while never
waking live, because it read `scores.get(path)` instead of `max(scores.values())`.

| Path | Status |
|------|--------|
| `launchctl bootstrap`, surviving logout and login | outstanding |
| Menu-bar rendering and its confirm dialog | rendering owner-confirmed per `STATE.md`; confirm dialog outstanding |
| Full voice loop end to end | wake word confirmed live per `STATE.md`; full loop outstanding |
| Desktop-control OS actions (Spotify, Terminal, OMC) | outstanding. AppleScript compiles clean and the resolver ran against the real repo tree |
| Background reasoning's spoken ping | outstanding |
| A real graph-refresh pass against the live graph | outstanding |
| Echo-cancellation bench | run on real hardware; numbers above |

The most likely first launchd failure, with its log signature: `python -m edith.daemon` always builds
a real `VoiceIO` and exits 1 without the `[voice]` extra, which under `KeepAlive` becomes a
10-second respawn loop in `~/.edith/logs/edithd.err.log`.

## Verification convention

Test-first, red to green. Every spec in `docs/specs/` carries a Completion Record stating the exact
verification that shipped with it, including what was live-smoked and what was not. When a claim here
and a Completion Record disagree, the code wins and both should be corrected.

Two recurring failure modes this repo has actually suffered, worth knowing before you trust a green
suite or a branch listing:

- **Tests green is not works.** See the wake-loop example above.
- **Commits strand on merged branches.** An agent keeps committing to a branch after its PR merges,
  and the work is invisible to ancestry checks. It has happened twice. Delete merged branches
  promptly, and diff by content rather than hash when auditing.
