# Voice — Conversation Mode (build brief)

> Voice-UX enhancement on top of Slice 3 (Voice) + Slice 5 (Router). Not a north-star slice;
> it makes the live voice loop feel like a conversation instead of a walkie-talkie. Owner-chosen
> scope (2026-07-12) over full open-mic addressee detection — see §Why not full open-mic.

## Purpose

Today: every query needs "Hey Edith", capture is a fixed 5 s window (pauses cut you off), and the
harness calls the Router with **zero history** (no cross-turn context). Conversation mode fixes all
three + adds a mute toggle:

```
"Hey Edith, how are you?"  → reply
   → [mic stays HOT ~10s]  → "and what about X?"   (NO wake word)  → reply WITH prior-turn context
   → [silence]             → conversation closes, back to wake-gated
mute toggle → hard-close the mic anytime
```

## Components (build each behind a testable seam; live audio is owner-smoke)

### 1. Follow-up window (no re-waking mid-conversation)
- After a reply is spoken, enter a `CONVERSING` state with a deadline (`EDITH_FOLLOWUP_SECONDS`,
  default ~10 s). While in it, a captured utterance is treated as a follow-up **without** a wake.
- On silence past the deadline → back to `IDLE` (wake-gated). Any real utterance resets the deadline.
- **Implement as a pure state machine** (like `edith/voice/live.py::_gate_action`) — `IDLE`/`CONVERSING`
  + timer — so it's unit-tested; only the mic reads stay in the untestable shell.
- Interaction with the half-duplex gate: the follow-up window starts AFTER `is_speaking` goes false
  (i.e. after the cooldown), so it never captures EDITH's own tail.

### 2. VAD / endpointing (stop cutting off on pauses)
- Replace the fixed `_UTTERANCE_SECONDS = 5.0` capture with **silence-based endpointing**: capture
  until ~800 ms of trailing silence (or a hard max, e.g. 15 s).
- **Recommendation: energy-based first (no new dep)** — RMS threshold over frames, end on N silent
  frames. `webrtcvad` is more robust but adds a dep; add it only if energy proves flaky.
- Reuse the existing 1280-sample/80 ms frame loop. The RMS heartbeat already in `live.py`
  (`EDITH_VOICE_DEBUG`) gives the threshold calibration data.

### 3. Conversation memory ("unlimited context" — the highest-value piece)
- **Route `voice.utterance` through `Brain`, not the raw Router.** `edith/brain/loop.py::Brain`
  already: recalls relevant Facts from Memory, assembles context, redacts, calls the Router,
  and **remembers the exchange**. The voice harness (`edith/voice/__main__.py`) currently calls
  `router.model_call` directly with no history — that's the gap.
- Build a `Brain` wired to `VectorMemoryStore(~/.edith/data/memory.kuzu)` + the Router + `voice.speak`.
  This is the (scoped) **daemon-integration** step. Reuse the wiring already in
  `edith/daemon/edithd.py` (it builds Brain + skills + resolve-on-miss); consider just running the
  real `edithd` with a live `VoiceIO` instead of the standalone harness (that's the composition-root
  payoff — see STATE "daemon-integration gap").
- **Recent-turns buffer:** Brain's semantic recall won't reliably surface the *immediately prior*
  turn for tight follow-ups ("and what about X?"). Add a short in-session rolling buffer (last ~6
  turns) prepended to the messages verbatim — north-star's "working context buffer". Persisted turns
  (Brain already `remember()`s them) give the durable/semantic half; the buffer gives the literal half.
- **Honest framing:** this is north-star's "unlimited context" = memory + retrieval + (later) compaction,
  NOT an infinite window. `Memory.compact()` is still a deferred seam; the rolling buffer is bounded.
- ⚠ **Kuzu is single-process** — the viewer/finder/ingest must be closed while the voice loop holds
  the graph. (Prod fix, noted in STATE: route all DB access through `edithd`.)

### 4. Mute toggle
- A hard mic close/open the owner controls. **Reuse `VoiceIO.set_paused()`** (already exists;
  suppresses utterances). Bind it to either a terminal keypress (e.g. `m`) or the Control API
  `pause`/`resume` (already wired to `set_paused` in `edithd`). Recommend the Control API path if
  running via `edithd` (menu-bar already speaks to it); keypress if standalone harness.

## Why NOT full open-mic + addressee detection (v1)
"Is this utterance addressed to EDITH?" is unsolved-ish — it's why Alexa/Google keep a wake word.
Always-on transcription + a per-utterance intent classifier is a token-burner (north-star §6.2) and a
privacy surface. The follow-up window delivers ~90% of the "always listening" feel without that cost.
Revisit only after conversation mode proves out.

## Build steps (ordered)
1. `Brain` wiring in the voice path (component 3) — the highest-value, mostly headless-testable piece.
   Add the recent-turns buffer. Verify multi-turn context with a fake Router + in-memory store.
2. Follow-up state machine (component 1) — pure `_conversation_state()` helper + unit tests; wire into
   the `live.py` loop.
3. Energy endpointing (component 2) — replace fixed capture; calibrate RMS threshold via debug readout.
4. Mute toggle (component 4).
5. Owner live-smoke: a real back-and-forth — follow-ups without re-waking, no cut-off on pauses,
   context carried across turns, mute works.

## Verification
- Headless: follow-up state machine (IDLE↔CONVERSING + timeout), Brain multi-turn context (turn 2
  sees turn 1), endpointing decision (ends on silence, respects hard max). Full suite + ruff + pyright.
- Owner live-smoke (mic/speaker): the §Purpose flow end to end.

## Gotchas (this project's recurring bites)
- **Verify independently + live-run** — tests green ≠ works (the wake loop passed 161 tests but never
  woke live; the half-duplex fix got orphaned off an already-merged PR). Confirm the merge lands on master.
- Kuzu single-process (above). Guard still deferred (authorize/budget are allow-by-default seams).
- Keep replies SHORT (already enforced: ≤2 sentences, max_tokens 120) — long TTS + endpointing +
  follow-up windows compound latency.

---

## Build record — 2026-07-14 (Session 17, team fan-out)

Built by a 3-agent team (disjoint headless units) + lead integration, on
`feat/voice-conversation-mode` off `master`.

- **What shipped (all 4 components):**
  1. **Conversation memory** — `edith/brain/history.py::TurnBuffer` (rolling last-6 turns) +
     an optional `history` seam in `Brain` (splices prior turns between system and utterance,
     trails the exchange, redacts). The **voice harness** (`edith/voice/__main__.py`) uses
     `TurnBuffer` directly via a new pure `build_messages(system, history, text)` helper, so a
     follow-up ("and what about X?") resolves with the prior turn — while keeping the JARVIS
     "sir" persona and the direct sir-tuned model call.
  2. **Follow-up window** — `edith/voice/conversation.py::ConversationWindow` (pure IDLE↔CONVERSING
     state machine + timer). Wired into `live.py`: after a reply finishes (`_gate_action`→"flush")
     the window opens; while open, speech ENERGY starts an utterance with NO wake word.
  3. **Silence endpointing** — `edith/voice/endpointing.py::Endpointer` (energy/RMS + trailing-silence
     run, hard-max cap). Replaces the fixed 5 s capture in `live.py` (`_capture_endpointed`), so a
     pause no longer cuts the owner off.
  4. **Mute toggle** — `_start_mute_toggle` in the harness: `m`+enter toggles `VoiceIO.set_paused`
     (spec §4 reuse, daemon stdin thread).

- **Key decision (advisor-backed):** conversation memory is wired via the **in-session `TurnBuffer`
  in the standalone harness**, NOT by routing the harness through `Brain`+real Kuzu. Routing through
  Brain only earns its keep with the real store, and opening Kuzu from `__main__` re-introduces the
  multi-owner DB anti-pattern STATE.md warns against. So Brain's `history` splice is a **tested seam**
  the `edithd` composition root consumes next (see Deferred).

- **Verification:** full suite green + ruff + pyright clean. Headless-tested: `TurnBuffer`,
  `Brain` multi-turn context (turn 2 sees turn 1), `ConversationWindow`, `Endpointer` (incl. the
  "a pause shorter than silence_ms does NOT end" property), and `build_messages` ordering. The
  `live.py` wiring (follow-up onset, endpointed capture, flush→on_reply_finished) is **owner
  LIVE-SMOKE only** — the decision logic it calls is unit-tested, but the audio path is not.

- **DEFERRED (explicit next task):** **route the voice path through `Brain` + the real
  `VectorMemoryStore` in the `edithd` composition root** — this adds semantic/graph recall +
  cross-session `remember()` on top of the in-session buffer, at the correct single-owner venue.
  This is the long-standing "she talks back / daemon-integration gap." Brain's `history` seam and
  `TurnBuffer` are already built and tested for it.

- **Calibration owed:** `Endpointer` threshold (default RMS 500) needs live tuning against the
  `EDITH_VOICE_DEBUG` heartbeat; env knobs `EDITH_FOLLOWUP_SECONDS`, `EDITH_ENDPOINT_SILENCE_MS`,
  `EDITH_ENDPOINT_MAX_MS`, `EDITH_ENDPOINT_THRESHOLD` tune it with no recompile.
  - **Wake→command pause (live-calibration watch item, from review):** capture starts at the
    wake frame, so a long pause between "Hey Jarvis" and the command could trip the 800 ms
    trailing-silence rule and drop the command. If it bites in smoke, bump
    `EDITH_ENDPOINT_SILENCE_MS`, or (follow-up) start the endpointer clock on the first
    post-wake speech frame rather than the wake frame.

- **Review (code-reviewer, REQUEST_CHANGES → fixed):** startup greeting (and any unsolicited
  `speak`) no longer opens the follow-up window — `on_reply_finished` is gated on a real preceding
  captured utterance (`saw_utterance` latch), closing the ambient-capture-at-launch regression.
  `ConversationWindow.reset()` is now wired: while muted (`VoiceIO.is_paused`) the loop closes the
  window and skips capture. Bridged `_on_wake` exceptions are logged (no silent swallow).
  `max_tokens` aligned to the documented 120. Redaction, self-echo defense, thread-safety, and
  unbounded-growth all reviewed clean.
  **⚠ SUPERSEDED (2026-08-17):** the `saw_utterance` latch described above has since been DELETED.
  It was an uncorrelated boolean (it didn't know WHICH utterance/reply it was latching onto), which
  under overlapping TTS could open follow-up off the wrong speech or miss it entirely. Replaced by
  the `speak`/`speak_response` split + one-shot per-response completion signal — see the
  **Follow-up record — 2026-08-17** below for the current mechanism. Kept here for history only.

---

## Follow-up record — bounded voice behavior + concurrency hardening — 2026-08-17

Replaces the `saw_utterance` latch (Build record above, now superseded) with a correlated,
per-response completion signal, and closes three concurrency gaps found in review.

- **`speak` vs `speak_response` (the arming split):** `VoiceIO` now exposes two seams. Raw
  `speak()` — startup greeting, session narration, **and background-reasoning pings
  (`brain.background_done`)** — never arms follow-up: none of these are a reply to something the
  owner just said, so opening a 10s wake-free window for them would be ambient capture at an
  unpredictable moment. `speak_response()` — a skill's acknowledgement AND its final reply, a plain
  Brain answer — arms it, because these ARE direct replies the owner should be able to talk back to.
- **Per-speech records, not one global flag:** `VoiceIO` tracks a list of `_SpeechRecord`s (one per
  in-flight/recent `speak`/`speak_response` call) instead of a single `_active_handle` +
  `_response_active` pair. `is_speaking` stays `True` until EVERY tracked record is idle, so
  overlapping TTS (e.g. an ack racing its own final reply, or narration racing a response) can never
  orphan an earlier call's completion.
- **One-shot response completion, retained until fully idle:** a response's completion arms an
  internal flag the instant it genuinely finishes (stream `done()` + cooldown elapsed), but
  `consume_followup_ready()` only surfaces (and clears) it once `is_speaking` would report `False` —
  an overlapping narration or a second response cannot erase or prematurely expose it. Two
  overlapping responses collapse to a single follow-up opening (correct — one window is enough).
- **Overlapping-TTS + failure cleanup:** barge-in stops EVERY tracked handle (not just the most
  recent) and clears any not-yet-consumed follow-up signal, so a stale completion from before an
  interruption can't leak into the interrupted turn. A record is registered *before* `tts.speak()`
  is awaited (so a barge-in or the stall ceiling can invalidate it before its handle exists); if
  `tts.speak()` raises or is cancelled, the pending record is removed and the mic is never
  permanently gated; a handle that arrives late (after barge-in or the ceiling already dropped its
  record) is stopped immediately instead of being adopted or left to play untracked.
- **Missed-edge handling:** `edith/voice/live.py::_followup_poll` consumes the one-shot signal once
  per loop poll, but never while the gate action is `"skip"` (still speaking) — it only reads (and
  the loop only acts on) the signal once the gate is idle, so a response that finishes entirely
  between polls (the speaking→idle edge itself missed, e.g. during a blocking capture/transcribe)
  still opens follow-up on the next poll instead of being silently dropped.
- **Timing:** `VoiceIO`'s speak cooldown is **0.3s** (`_SPEAK_COOLDOWN`, down from 2.5s) and the
  live loop's post-speech tail flush is **0.3s** (`_SPEAK_FLUSH_SECONDS`, computed via
  `_frames_for_seconds` with `math.ceil` so 0.3s at 16 kHz/1280-sample frames rounds up to 4 frames,
  not down to 3). Follow-up window stays **10s** (`EDITH_FOLLOWUP_SECONDS`, unchanged).
- **VPIO/Speex remains bench-only:** none of the above wires real echo cancellation into the
  shipped half-duplex loop — `edith/voice/duplex/` (VPIO) and `edith/voice/aec_bench/` (SpeexDSP)
  are still an isolated comparison bench (see `docs/ENGINEERING-NOTES.md::"The half-duplex mic gate,
  and the duplex spike"`), not a `run_live_loop` dependency. This fix only tightens the shipping
  half-duplex gate's timing and correctness.
- **Files:** `edith/voice/io.py` (`_SpeechRecord`, cooldown, `speak`/`speak_response`,
  `consume_followup_ready`), `edith/voice/live.py` (`_followup_poll`, `_frames_for_seconds`),
  `edith/daemon/edithd.py` (`_speak_decision`/`_speak_background` seam wiring).
- **Verification:** TDD throughout (RED before each production edit) across three fix rounds —
  overlap/race regressions, pending-record cleanup on exception/cancellation/stall, and the barge-in
  stale-signal + flush-rounding minors. Full suite + `pyright` + `ruff` clean after each round.
