# 03 — Voice (Wake Word + STT + TTS)

> **Honest-framing reminder:** no unicorns. "Unlimited context" = memory + retrieval +
> compaction; "two agents in one inference / haiku talks while opus thinks" = orchestration of
> two model calls (fast masks slow). If a section here implies a capability that doesn't exist,
> fix the section.
>
> This slice follows the shape below. Architecture-level interfaces + cross-cutting rules are
> fixed in `00-north-star.md` — **do not restate them, reference them.** This file adds
> *implementation* depth for this slice only.

## Terminology (glossary)

| Term | Meaning |
|------|---------|
| **EDITH** | The system. Always-on local-first macOS assistant. |
| **edithd** | The daemon process that runs everything under the hood. |
| **bus** | In-process event/message bus; components `publish`/`subscribe`. |
| **Guard** | Cross-cutting enforcement: `redact`, `authorize` (allow/ask/deny), budget. |
| **Router** | `model_call(messages, tier_hint) -> response` over the Bifrost adapter. |
| **Memory** | Graph + vector store: `recall` / `remember` / `compact`. |
| **SessionBus** | Watches OMC / Claude Code terminals → `session.event` / `session.state`. |
| **Skill** | Capability with `name`, `triggers`, `needs_confirmation`, `run(context)->result`. |
| **tier** | Model size class the Router selects: haiku / sonnet / opus. |
| **VoiceIO** | This slice. Owns mic-in and speaker-out; emits events, provides `speak()`. |
| **barge-in** | Owner speaks while EDITH is speaking; TTS stops immediately. |

---

## Purpose

Slice 3 wires EDITH's ears and mouth: always-listening local wake-word detection, local
speech-to-text, and pluggable text-to-speech output. It ships a complete audio round-trip —
owner says "Hey EDITH, [command]" and EDITH speaks back — integrated with the internal bus so
the Brain can consume utterances and drive TTS without knowing anything about audio hardware.

---

## Scope

**In:**
- Wake-word detection (`openWakeWord`, local, always-on)
- STT transcription (`faster-whisper`, local, triggered after wake)
- TTS output via pluggable adapter (ElevenLabs streaming primary; Piper/XTTS local fallback)
- Barge-in / interruption (new wake event cancels active TTS)
- Publishing `voice.wake` and `voice.utterance` on the bus; providing `speak(text)` to the Brain
- Latency budget tracking for the audio path

**Out:**
- Brain logic, skill dispatch, model calls — owned by Slices 1 + 5
- Session awareness (Slice 4), desktop control (Slice 6)
- ElevenLabs voice cloning or fine-tuning — the adapter takes a voice ID from config; building
  or cloning a voice model is out of scope

---

## Audio Pipeline

```
  ┌─────────────────────────────────────────────────────────────────────┐
  │  VOICE IO (Slice 3)              always-listening thread            │
  │                                                                     │
  │  ┌─────┐   raw PCM    ┌──────────────┐  wake    ┌───────────────┐  │
  │  │ MIC │ ──────────► │ openWakeWord  │ ──────► │  faster-      │  │
  │  │ (PA)│             │ "Hey EDITH"   │         │  whisper STT  │  │
  │  └─────┘             └──────────────┘         └──────┬────────┘  │
  │                                                       │ transcript │
  │                                                       ▼            │
  │                                              publish(voice.wake)   │
  │                                              publish(voice.        │
  │                                                utterance, text)    │
  └──────────────────────────────────┬──────────────────────────────────┘
                                     │  internal bus
                                     ▼
  ┌──────────────────────────────────────────────────────────────────┐
  │  BRAIN (Slice 1)   consumes voice.wake / voice.utterance         │
  │  decides response  ──► Router (Slice 5) ──► model call           │
  │                        (fast haiku ack + slow opus answer)       │
  └──────────────────────────────┬───────────────────────────────────┘
                                 │ speak(text)
                                 ▼
  ┌──────────────────────────────────────────────────────────────────┐
  │  VoiceIO.speak(text)   TTS adapter                               │
  │                                                                  │
  │  ┌───────────────────────┐       ┌──────────────────────────┐   │
  │  │ ElevenLabs streaming   │  OR   │ Piper / XTTS (local)     │   │
  │  │ (PRIMARY, cloud)       │       │ (FALLBACK / offline)     │   │
  │  │ streaming PCM chunks  │       │ full audio then play      │   │
  │  └──────────┬────────────┘       └─────────────┬────────────┘   │
  │             └────────────┬────────────────────-┘                │
  │                          ▼                                       │
  │                    ┌──────────┐                                  │
  │                    │ SPEAKER  │  (PyAudio / sounddevice)         │
  │                    └──────────┘                                  │
  └──────────────────────────────────────────────────────────────────┘
```

Wake and STT are always local. TTS primary (ElevenLabs) is a cloud call; TTS fallback
(Piper/XTTS) is fully local. The adapter selects the engine at startup from config.

---

## Interface to edithd

The VoiceIO contract from north-star §4.3, implemented here:

- **Inputs received:**
  - `speak(text: str) -> None` — called by Brain; queues text for TTS playback
  - `voice.wake` subscribed internally to trigger barge-in cancellation

- **Outputs published to bus:**
  ```
  voice.wake      { topic: "voice.wake",      ts, source: "voice_io", payload: {} }
  voice.utterance { topic: "voice.utterance", ts, source: "voice_io",
                    payload: { text: str, confidence: float } }
  ```

- **Bus events published:** `voice.wake`, `voice.utterance`
- **Bus events consumed:** none from the bus directly — barge-in is internal to VoiceIO
  (new wake word cancels active playback before re-publishing `voice.wake`)
- **Control contracts:** none; VoiceIO does not surface a Control API command. `pause` from
  the Control API reaches VoiceIO via an internal flag the daemon sets; VOICE keeps listening
  for the wake word even while paused so `resume` can be voice-triggered.

---

## Data model

Stateless. VoiceIO holds only:
- A reference to the active TTS playback handle (for cancellation on barge-in)
- A boolean `paused` flag (set by daemon on `pause`/`resume` Control commands)
- Loaded wake-word model weights (openWakeWord, in-process)
- Loaded STT model weights (faster-whisper, in-process)

Nothing is persisted to disk. Transcripts are handed to the bus and owned downstream.

---

## Dependencies

- **Other slices:** Slice 1 (Brain / bus) must exist — `voice.wake` and `voice.utterance`
  need a subscriber before VoiceIO is useful. VoiceIO itself has no upstream slice dependency
  at the audio layer.
- **Libraries:**
  - `openWakeWord` — local wake-word detection; custom "Hey EDITH" / "EDITH" model
  - `faster-whisper` — local Whisper inference (CTranslate2 backend; no GPU required)
  - `elevenlabs` (official Python SDK) — streaming TTS via ElevenLabs API
  - `piper-tts` or `TTS` (coqui) — local neural TTS fallback
  - `pyaudio` or `sounddevice` — mic capture and speaker playback (macOS CoreAudio)
  - `numpy` — PCM buffer handling between capture and model input

---

## Tech choices

| Choice | Justification |
|--------|--------------|
| **openWakeWord** (custom model) | Local, no cloud dependency, supports custom wake phrases. Training a small "Hey EDITH" model is well-documented and runs on CPU. Alternatives (Picovoice Porcupine) are commercial and require a separate license key. |
| **faster-whisper (medium or small.en)** | 3–8× faster than vanilla Whisper on CPU via CTranslate2. No GPU required on the owner's machine. Privacy: audio never leaves the machine before the Brain sees it. `large-v3` is available but overkill for short voice commands; start with `small.en` and profile. |
| **ElevenLabs streaming (primary TTS)** | Achieves time-to-first-audio ~400–700 ms via chunked streaming; matches the MCU-Jarvis quality bar. Official Python SDK handles streaming audio chunks. Voice ID is a config value — see legal flag below. |
| **Piper TTS (local fallback)** | Fast local inference, does not require ollama (which is absent on owner's machine). XTTS is higher quality but slower; Piper is the right fallback for latency reasons. |
| **sounddevice over pyaudio** | sounddevice wraps PortAudio and has a cleaner async stream API; less prone to the callback-thread complexity issues that plague raw PyAudio on macOS. |

> **NOTE — ollama absent:** the north-star reality-check (§8) confirms ollama is not installed.
> Any local model path (whisper, Piper) must use native Python libraries, not an ollama endpoint.

---

## Latency budget

Target: **time-to-first-audio under 2 seconds** from end of owner's utterance.

```
  wake word detection  ~0 ms  (streaming, sub-frame latency)
  VAD / end-of-speech  ~300 ms (silence detection after last word)
  STT (faster-whisper) ~300–600 ms (small.en on CPU, typical command length)
  bus → Brain → Router ~100 ms (local, in-process)
  haiku fast-ack call  ~300–600 ms (Bifrost round-trip, short prompt)
  TTS first chunk      ~400–700 ms (ElevenLabs streaming latency)
  ─────────────────────────────────
  total (optimistic)   ~1.4 s
  total (realistic)    ~1.9 s
```

This is why Slice 5's two-call pattern matters: the fast haiku acknowledgement ("On it" /
"Looking now") fires as soon as STT completes — its TTS chunk starts playing while the slow
opus call is still running. The owner hears a response within ~1.5 s; the real answer follows
when opus finishes. Without the two-call pattern, opus latency (~3–8 s) would be directly
audible as dead silence.

Slice 3 owns the audio path latency only. Two-call mechanics are Slice 5's responsibility.

---

## Barge-in / interruption

When a new `voice.wake` is detected while TTS is playing:

1. VoiceIO cancels the active playback handle immediately (drains the audio buffer, stops
   the ElevenLabs stream or Piper process).
2. Publishes `voice.wake` on the bus as normal.
3. STT begins for the new utterance.

Brain is not involved in barge-in — it is handled entirely within VoiceIO before the event
reaches the bus. Brain simply receives a new `voice.utterance` and treats the prior response
as abandoned.

**Barge-in ≠ steering.** Barge-in only stops TTS *audio*; it never stops in-flight compute.
When a supervised reasoning session is active (opus running under a `SupervisedSession`, see
Slice 5 §Supervised reasoning), the new `voice.utterance` is *also* routed to
`SupervisedSession.steer()` so the sonnet arbiter can decide whether to cancel/redirect opus.
Cancelling audio and cancelling the reasoning task are two separate actions; VoiceIO owns the
first, the Router arbiter owns the second.

---

## Local vs. cloud split

```
  Component           Local   Cloud   Notes
  ──────────────────  ──────  ──────  ───────────────────────────────────────
  Wake word           YES             openWakeWord; nothing leaves machine
  STT                 YES             faster-whisper; audio stays on device
  TTS (primary)               YES     ElevenLabs; text (not audio) sent out
  TTS (fallback)      YES             Piper/XTTS; fully offline
```

Privacy boundary: raw audio and transcripts never leave the machine. Only the text string
passed to `speak()` is sent to ElevenLabs (after Guard redaction — see below).

---

## Legal flag — voice cloning

> **FLAG:** Reproducing the exact voice of the MCU Jarvis character (Paul Bettany's
> performance) via ElevenLabs voice cloning almost certainly requires a license that does not
> exist for personal/AI projects. Using a voice that is *stylistically similar* (British male,
> calm, precise) with an off-the-shelf ElevenLabs voice ID is legally clear. The pluggable
> adapter keeps the voice ID a config value (`ELEVENLABS_VOICE_ID` in the Keychain / `.env`)
> — it is not load-bearing. The build step is: pick an appropriate off-the-shelf ElevenLabs
> voice, set the ID in config, and accept that it approximates the style, not the character.

---

## Autonomy & secrets notes

- **Autonomy gate** (north-star §6.3): VoiceIO performs no autonomous actions — it only
  captures and plays audio. `speak()` is initiated by Brain, which handles confirmation gates.
  VoiceIO is always AUTO; nothing here triggers the ASK flow.

- **Secrets:**
  - `ELEVENLABS_API_KEY` — lives in macOS Keychain via `keyring`; loaded into memory at
    process start, never logged, never written to the bus or graph.
  - `ELEVENLABS_VOICE_ID` — not a secret (it's a public voice identifier) but stored in
    config / `.env` for swappability.
  - Transcripts carry owner speech and may contain sensitive content. They are published on
    the bus as `voice.utterance.payload.text`. Guard's never-persist list (north-star §6.1)
    must cover transcript text: transcripts are consumed by Brain and discarded; they are
    NOT written to the Memory graph or vector store verbatim.
  - `speak(text)` receives Brain-constructed response text. Guard's `redact()` must run on
    this text before it is sent to ElevenLabs — a Brain response could echo back a secret
    that was in context.

---

## Cost / token notes

VoiceIO itself makes no model calls. Its cost surface is ElevenLabs character billing:

- **ElevenLabs charges per character** of TTS input. Every `speak()` call has a cost.
- **Gate long TTS:** Brain should not pass a 2,000-word essay to `speak()`. Either the Brain
  truncates to a spoken summary before calling `speak()`, or VoiceIO enforces a hard character
  cap (e.g. 500 chars) and logs a warning for overflow.
- **Fallback to local:** when ElevenLabs is down or the API key is absent, the local Piper
  fallback kicks in automatically — zero marginal cost, lower quality.
- **STT is free** (local CPU inference).
- **Wake word is free** (local, always-on, CPU).

---

## Build steps (high-level, ordered)

1. **Environment setup** — add `openWakeWord`, `faster-whisper`, `elevenlabs`, `piper-tts`,
   `sounddevice`, `numpy` to `pyproject.toml` under `[project.dependencies]`; `uv sync`.

2. **Mic capture loop** — implement a `sounddevice` input stream that feeds 16 kHz mono PCM
   chunks to the wake-word model in real time. Verify mic access on macOS (check
   `Privacy & Security → Microphone` for the terminal / launchd process).

3. **Wake-word model** — download or train an `openWakeWord` model for "Hey EDITH" / "EDITH".
   A pre-built custom model can be trained with the openWakeWord training notebook using ~30
   positive samples. Wire detection into the capture loop; on detection, publish `voice.wake`
   and start the STT window.

4. **STT transcription** — on wake, buffer audio until end-of-speech (VAD via `silero-vad`
   or faster-whisper's built-in VAD), run `faster-whisper` on the buffered segment, publish
   `voice.utterance` with the transcript and confidence score.

5. **TTS adapter interface** — define a `TTSAdapter` abstract base with `speak(text) ->
   None`; implement `ElevenLabsAdapter` (streaming) and `PiperAdapter` (local). Config flag
   `TTS_ENGINE=elevenlabs|piper` selects the adapter at startup.

6. **ElevenLabs streaming adapter** — use the official `elevenlabs` SDK's streaming endpoint;
   pipe audio chunks to `sounddevice` output stream as they arrive for minimal
   time-to-first-audio.

7. **Piper local adapter** — invoke `piper` subprocess with text input; pipe output PCM to
   `sounddevice`.

8. **Barge-in** — in the mic capture loop, check whether the playback handle is active on
   each wake detection; if so, cancel it before publishing the new `voice.wake`.

9. **Guard integration** — run `Guard.redact(text)` on the `speak()` argument before passing
   it to the TTS adapter.

10. **Pause/resume hook** — expose a `set_paused(bool)` method on VoiceIO; daemon calls this
    when it receives `pause`/`resume` from the Control API. While paused, STT and skills are
    suppressed but wake detection remains active so `resume` can be voice-triggered.

11. **Integration smoke test** — wire VoiceIO into `edithd`'s bus (or a test harness with a
    stub bus); say "Hey EDITH, what time is it"; verify `voice.wake` then `voice.utterance`
    on the bus; call `speak("It's three forty-five PM")` and hear audio.

---

## Verification / testing

```bash
# 1. Standalone wake-word test (no bus required)
python -m edith.voice.wakeword_test
# Expected: prints "WAKE DETECTED" within 1s of saying "Hey EDITH"

# 2. STT round-trip
python -m edith.voice.stt_test
# Speak a sentence after prompt; expected: transcript printed to stdout within 2s of silence

# 3. TTS ElevenLabs adapter
ELEVENLABS_API_KEY=$(keyring get edith elevenlabs_key) \
  python -m edith.voice.tts_test --engine elevenlabs --text "Awaiting your instructions."
# Expected: audio plays within 1s of command

# 4. TTS Piper fallback
python -m edith.voice.tts_test --engine piper --text "Awaiting your instructions."
# Expected: audio plays (higher latency than ElevenLabs is acceptable)

# 5. Barge-in
# Start a long TTS playback, say "Hey EDITH" mid-sentence
# Expected: audio cuts off; new voice.wake event appears on bus

# 6. Guard redaction on speak()
# Pass text containing a known redaction trigger (e.g. a fake API key pattern)
# Expected: Guard strips it before ElevenLabs call; verify via ElevenLabs API log
# that the key string is absent from the request payload
```

Latency check: time from end of utterance to first audio byte — target < 2 s for haiku-ack
path. Measure with `time.perf_counter()` around the STT → bus → speak() → first-chunk cycle.

---

## Open questions

- **openWakeWord custom model training data:** 30 positive samples ("Hey EDITH" / "EDITH") is
  the documented minimum. Does owner want to record them, or use TTS-synthesized samples as a
  bootstrap? Synthesized samples reduce false-negative rate less well than real recordings —
  owner decides.

- **faster-whisper model size:** `small.en` is the recommended starting point for latency.
  If transcription accuracy on short technical commands (repo names, PR numbers) is
  insufficient, bump to `medium.en`. Profile on owner's machine before deciding.

- **VAD strategy:** faster-whisper has built-in VAD; silero-vad is an alternative with more
  tuning knobs. Built-in is fine for v1 but may mis-segment on ambient noise in a home office.
  Monitor false-cuts during early testing.

- **macOS mic permissions for launchd:** a `launchd`-launched daemon may not hold a mic
  permission grant inherited from the terminal. Needs verification — may require a separate
  NSMicrophoneUsageDescription or a permission-priming step on first launch.

- **ElevenLabs voice selection:** owner needs to pick a voice ID from the ElevenLabs library
  that matches the desired British-male-Jarvis-style. Recommend auditing a shortlist of
  ElevenLabs "British Male" voices and setting `ELEVENLABS_VOICE_ID` in config. The model
  does not prescribe the ID.

---

## Completion Record — Voice — 2026-07-08 (Session 13)

- **What shipped:** `edith/voice/` — the VoiceIO layer with all hardware/ML behind injectable
  seams so the test suite runs with ZERO heavy deps. `TTSAdapter` ABC + `TTSHandle` protocol;
  `ElevenLabsAdapter` (streaming, primary) + `PiperAdapter` (local fallback) with `select_adapter`;
  `VoiceIO` (bus-wired speak/wake orchestrator); edithd wiring; and three CLI smoke harnesses
  (`tts_test`, `wakeword_test`, `stt_test`) for the owner's live audio test. Built by an OMC
  tmux team (3 executor workers, lead-orchestrated) — see BUILD_LOG Session 13.
- **How it works:** `VoiceIO.speak(text)` runs `sanitize_text` (Guard redaction) FIRST, enforces a
  500-char cap, then calls the injected `TTSAdapter.speak`, retaining the returned `TTSHandle` for
  barge-in. The wake path stops any active handle (barge-in), publishes `voice.wake`, then publishes
  `voice.utterance {text, confidence}` — suppressed while paused (wake always fires so resume can be
  voice-triggered). Heavy libs (`elevenlabs`, `sounddevice`, `piper`, `openwakeword`,
  `faster-whisper`) are imported INSIDE adapter/harness methods, never at module top, and gated
  behind an optional `[voice]` extra. edithd gained an optional `voice: VoiceIOLike | None` param:
  when wired it threads `voice.speak` into `PRReviewSkill` (findings are spoken) and mirrors Control
  API pause/resume into `voice.set_paused` via new `on_pause`/`on_resume` ControlServer callbacks
  (same seam pattern as `on_kill`). Default `voice=None` → behaviour unchanged.
- **Key decisions made during build:** openWakeWord prebuilt **"hey jarvis"** model to start (skips
  the 30-sample custom-training blocker); `faster-whisper small.en`; Piper local fallback;
  `sounddevice`. All hardware/ML behind seams — the honest split is "core is unit-tested; audio is
  owner-live-smoked."
- **Deviations from spec + why:** (1) No haiku *model* call for the ack (that's the Slice-5 two-call
  pattern); VoiceIO just plays what Brain hands `speak()`. (2) Barge-in steering into a
  SupervisedSession (spec §Barge-in) is deferred to Slice 5 — VoiceIO owns only the audio-cancel
  half today. (3) Actual mic capture / wake-model load / whisper inference / audio playback are seam
  stubs, not implemented — they need hardware and are the owner's live-smoke surface (below).
- **Files created / changed:** NEW `edith/voice/{__init__,tts,adapters,io,tts_test,wakeword_test,
  stt_test}.py`; EDIT `edith/daemon/edithd.py` (voice wiring), `edith/daemon/control.py`
  (on_pause/on_resume callbacks), `pyproject.toml` ([voice] extra). Tests: NEW
  `tests/test_voice_{tts,adapters,io,wiring}.py`.
- **Verification / tests run + results:** 161 passed + 1 skipped (was 135+1; +26 voice tests:
  4 tts + 13 adapters + 5 io + 4 wiring), `ruff` clean, `pyright` 0 errors, **zero `# type: ignore`**
  in the new source (lead fixed one `[assignment]` suppression at root by typing the default Piper
  runner as the `_PiperProcess` protocol, and one monkeypatch of RuntimeState by replacing it with
  the ControlServer `on_pause`/`on_resume` callback seam). Non-vacuous redaction test in
  `test_voice_io.py` (planted `sk-bf-…` present in raw, absent from what the TTS adapter received).
  CLI harnesses smoke cleanly WITHOUT the voice extra (print an install message, exit 0).
- **Follow-ups / known gaps (owner LIVE-SMOKE surface — cannot be verified headlessly):**
  - Install the audio stack: `brew install portaudio` then `uv pip install -e '.[voice]'` (heavy:
    ctranslate2, onnxruntime).
  - Add `ELEVENLABS_API_KEY` + `ELEVENLABS_VOICE_ID` to `.env` / Keychain (never in chat). Pick a
    British-male voice ID (spec §Legal flag — style, not the copyrighted character).
  - **Broaden `sanitize_text` coverage BEFORE enabling real ElevenLabs TTS.** `VoiceIO.speak`
    redacts in the right place (before the adapter), but `sanitize_text` catches only known shapes
    (`sk-bf-`/`GOCSPX-`/refresh-token/`gho_`). Unlike Slice 2 (a miss reached Opus via the Bifrost
    *proxy*), a miss here sends text to **ElevenLabs, a third-party cloud** — a real exfiltration
    surface and an org "never leak secrets" concern. The path is inert today (no key, no `[voice]`
    extra, stub seams), but this must be widened before live TTS is switched on.
  - Grant macOS mic permission to the terminal/launchd process (spec open question).
  - Implement the real seam bodies: mic capture (sounddevice stream), openWakeWord detection loop,
    faster-whisper STT, and audio playback — then run `python -m edith.voice.{wakeword,stt,tts}_test`.
  - Slice-5 barge-in→SupervisedSession steering; haiku two-call ack.
