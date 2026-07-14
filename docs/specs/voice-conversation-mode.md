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
