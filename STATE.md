# EDITH — Build State

> Machine-and-human readable status. Update this at the end of every session (or at ~90% context).
> This is the first file a new session reads after `SESSION-PROTOCOL.md`.

**Current phase:** Slices 0–6 DONE + Viewer + Ingest + NL-finder + Workspace-graph + voice conversation mode + **daemon composition root ("she talks back", Session 18)**. **ALL NUMBERED SLICES BUILT + the daemon now runs voice→graph.** Next deferred seams (owner-requested order): echo-cancellation improvement, **Guard** (authorize/budget), **Router background-opus** (`think_async`/`supervised_reason`), **`Memory.compact()`**.
**Session 18 (2026-07-17) — daemon composition root** on branch `feat/daemon-composition-root` (spec 10, PR pending). `python -m edith.daemon` boots the full daemon with a real `VoiceIO` on a shared bus → the graph-backed Brain answers voice with semantic recall + cross-session memory + all skills. Brain gained injectable `system_preamble`/`answer_max_tokens` + a **model-error seam** (catches `MODEL_CALL_ERRORS`, speaks an apology instead of going silent); daemon subscribes `brain.decision`→`voice.speak` (plain-answer path only, no double-speak); shared `edith/voice/persona.py`; injected `bus`. Tightened both `MemoryLike` protocols' `remember(edges)` to `list[Edge]` so a real `VectorMemoryStore` type-checks into the daemon. **302 tests, ruff + pyright clean.** `__main__` + live loop are owner-smoke; mic shutdown is Ctrl-C/process-exit (to_thread loop doesn't cancel). See `docs/specs/10-daemon-composition.md` §Completion Record.
**Active slice:** → all numbered slices done + voice conversation mode done. Session 16 (2026-07-12): built **Slice 6 (Desktop control)** advisor-first + TDD on branch `feat/slice-6-desktop-control` (PR-ready) — `edith/desktop/` (regex `parse_command` + filesystem `RepoResolver` + osascript/`open` executors behind an injected `Runner` seam) + `DesktopControlSkill` registered LAST in edithd's Brain. 264 tests + 2 skipped, ruff/pyright clean (code-reviewer REQUEST_CHANGES round folded in: return-code error path, Spotify escaping, hyphenated-repo parse, and a new `SkillResult.handled` flag so a broad-trigger skill can decline a turn and Brain falls through to the answer loop instead of dead-ending). Terminal.app `do script` drives BOTH the visible-terminal and the OMC-launch paths (claude is an interactive TTY REPL, won't run headless under `Popen` — spec Option-C deviation). `RepoResolver` prefers the flat `~/gitstuff/<name>` over the org-nested duplicate (bulk-clone artifact, verified identical remote); same-depth collisions still ASK. Safe live checks: parsed all spec examples, `osacompile`d the AppleScript clean, resolver ran against the real 1400-repo tree. **Owner LIVE-SMOKE of actual OS actions (Spotify/Terminal/OMC) still pending.** Prior that session: multi-org workspace graph (PR #10), voice self-echo/persona fixes (PRs #8/#12), SageMaker `hey_edith` retrain, Router Slice 5 (PR #7).
**Session 17 (2026-07-14) — voice conversation mode** on branch `feat/voice-conversation-mode` (PR #14, merged). Built by a 3-agent team + lead integration: `TurnBuffer` (in-session recent-turns memory) spliced into the voice harness via pure `build_messages`; `ConversationWindow` (follow-up window, no re-wake) + `Endpointer` (silence endpointing, no cut-off on pauses) wired into `live.py`; `m`+enter mute via `set_paused`. Brain's `history` splice built + tested as the seam for the DEFERRED next task: **route voice → Brain + real Kuzu in the `edithd` composition root** (semantic recall + cross-session remember — the "she talks back" daemon gap). Live-audio path is owner-smoke; decision logic unit-tested. See `docs/specs/voice-conversation-mode.md` §Build record. **Also found stranded off master:** commit `458f77f` (long-reply max_tokens=120 fix) never reached master via PR #12 — flag for its own recovery PR.
**Repo:** everything is on **`master`** (renamed from `main`, Session 11). All feature branches merged
+ deleted; `master` is the GitHub default. New work = branch off `master`, PR in. Full graph LIVE in
`~/.edith/data/memory.kuzu` (206 nodes: 23 Repo, 26 Person, 12 Project, 145 Fact — embedded, no leak).
**Prev session:** 2026-07-07 — Session 12 (built Slice 2 PR-review skill, TDD, delegated to Opus executor then verified independently + live-smoked). Added the Skill contract + Brain trigger-dispatch registry (didn't exist before — Brain went straight to model), `PRReviewSkill` 7-step flow, injectable async `gh` runner, and `Person.gh_handle` via a guarded non-destructive migration. Confirm-gate is the crux and is proven unreachable-on-deny by both a non-vacuous test and a live smoke (real gh + real Opus on `patterninc/agents#2423` → real review, `posted=False`, zero `pr review` writes). 130 tests + 1 skipped, ruff/pyright clean. Session 10 (prior): closed the ingest↔finder embedding gap, TDD. Ingest now writes via `VectorMemoryStore` so Facts are embedded on `remember` (Fix 1, `run_ingest(embedder=…)` seam); `VectorMemoryStore.backfill_embeddings()` + `python -m edith.ingest --reembed` embed existing graph-only Facts with the LOCAL embedder, no model calls, idempotent, credential-free (Fix 2); `find_repos` adds a per-token graph fallback that fires ONLY when both signals score zero, so a populated graph never silently returns nothing (Fix 3). 114 tests green, ruff/pyright clean. Live: `--reembed` embedded the 145 real Facts; `python -m edith.finder "seo tools"` now returns real repos (was "No repos matched"). Known limitation documented: Kuzu embedded is single-process (lock contention across viewer/finder/ingest; prod fix = route all DB access through `edithd`).

## Slice status

| # | Slice | Spec | Build | Notes |
|---|-------|------|-------|-------|
| 0 | North-star architecture | ✅ done | — | Authoritative doc |
| 1 | Memory + Brain | ✅ done | ✅ core done | Memory (Kuzu graph + sqlite-vec) + **bus** + **Router** (Bifrost, live-smoke green) + **Brain loop** + **edithd daemon** (unix-socket Control API pause/resume/kill/status, 0600, startup/shutdown ordering, pause-suspends-Memory, launchd plist template) — **59 tests + 1 live-skipped, ruff/pyright clean, 3-reviewer validated**. Documented seams left for their slices: `compact()`, Guard (budget/authorize), encrypted-volume mount, VoiceIO/SessionBus wiring. |
| 2 | PR-review skill | ✅ done | ✅ done | `edith/skills/` (Skill contract + `gh` runner + `PRReviewSkill`) + Brain trigger-dispatch + `Person.gh_handle` (guarded migration) + **registered in edithd's Brain** (`skills=[PRReviewSkill(router)]`; default `_silent`/`_deny` → dispatches but never posts until Slice 3 voice). Confirm-gate is the crux: `gh pr review` unreachable unless `confirm()==True`, default DENY. Diff redacted (known shapes) before Opus. **131 tests + 1 skipped, ruff/pyright clean.** LIVE-smoked: real gh + real Opus on `patterninc/agents#2423`, confirm=deny → real review, `posted=False`, zero `pr review` writes. **Known gap (verified live):** resolution against the real graph currently ALWAYS asks (ingested Persons have `gh_handle=""`; recall surfaces no Repo for a person name) — safe ask-when-unsure path; "instant HIT next time" is only partial. Diff-size cost gate NOT wired. See spec 02 §Follow-ups. |
| 3 | Voice | ✅ done | ✅ done | `edith/voice/`: TTSAdapter ABC + ElevenLabs/Piper adapters + VoiceIO (speak→redact→cap, wake/utterance bus events, pause-suppress, barge-in) + edithd wiring (speak→PRReviewSkill; pause/resume→set_paused via new ControlServer on_pause/on_resume callbacks) + CLI harnesses. Hardware/ML behind injectable seams → **161 tests + 1 skipped, ruff/pyright clean, zero type:ignore.** Built by OMC tmux team (3 workers). **Wiring seam is in place + unit-tested; the daemon does NOT speak yet** — every real path builds `EdithDaemon(voice=None)` (no composition root constructs a real `VoiceIO`, and mic/wake/STT/playback are stub seams). Audio path = owner LIVE-SMOKE. **Session 13 follow-up:** `[voice]` extra installed, `sanitize_text` broadened for the ElevenLabs egress (DONE), ElevenLabs adapter fixed to v2.56 API, and mic/wake/STT seam bodies implemented in `edith/voice/live.py` + `python -m edith.voice` (openWakeWord hey_jarvis + whisper small.en + sounddevice) — doc-derived, NOT hardware-verified. Owner runs the real audio smoke (mic/speaker/key). |
| 4 | Session awareness | ✅ done | ✅ done | `edith/session/`: **spike** (transcript-tail confirmed on the live machine — see `scratch/spike_session_tap_findings.md`) → **TranscriptCollector** (dep-free EOF-seek poller of `~/.claude/projects/**/*.jsonl`; primes to EOF so history is NOT replayed) → **SessionBus** (normalize→classify→**REDACT choke-point**→`session.event`/`session.state` + in-mem states map + Control API `last_event`) → **Narrator** (3-class policy: silent / spoken-local template / model-gated haiku; idle via `tick()`) → **SessionQuerySkill** ("what is session 2 doing?" via Brain dispatch, phrase triggers). edithd wires it all (`enable_session_awareness` flag gates the live tail off in tests). Hardened `sanitize_text` with a **connection-URI password** pattern (the killer-demo leak). **35 new tests (196 total +1 skipped), ruff/pyright clean.** LIVE-smoked on real transcripts: 11.7k events classified, real pasted Snowflake/Postgres creds → `[REDACTED]`, 0 leaks. **Cost gate (spec #5): per-session error-narration cooldown** — measured 452→72 model calls over the real stream (~0.6/session); Guard's real budget still deferred. Deviations (documented): Narrator is a collaborator (not in Brain); collector polls (not watchdog). |
| 5 | Router | ✅ done | ✅ done | `edith/router/`: `tiers.py` (`resolve_tier` + `TaskType`; owns the `Tier` enum now) — latency-first policy (Sonnet=live voice, Haiku=acks, Opus=explicit/background), override rules (ACK_FILLER→Haiku, HAIKU→Sonnet on size, OPUS budget-gated→Sonnet+`budget_limited`, deep signal→Sonnet+`suggest_background`). `bifrost.py`: `model_call_stream` (Anthropic SSE→`ModelChunk`), **`model_call_masked`** (tier-parameterized, answer defaults SONNET not opus; TRUE overlap — both requests fire before draining), `budget_check`+`redactor` seams, **redaction choke-point inside every `model_call*`**. Non-streaming POST unchanged (callers untouched). **17 new tests (212 total +1 skipped), ruff/pyright clean.** LIVE-smoked: `model_call_stream` vs REAL Bifrost yielded real tokens (SSE parser verified against actual stream). **Deferred/UNMET:** `supervised_reason` + `think_async`/auto-escalation (background opus — the philosophy's centerpiece; `suggest_background` returned but unacted); masking has no live consumer until VoiceIO `speak_stream` + composition root; OpenAI provider-swap config-only. |
| 6 | Desktop control | ✅ done | ✅ done | `edith/desktop/` (parser + `RepoResolver` + executors behind an injected `Runner` seam) + `DesktopControlSkill` (registered LAST in edithd, `needs_confirmation=False`, AUTO-only). "open Spotify", "play X on Spotify", "pause/skip/volume", "open a terminal in <repo>", "start OMC in <repo>". Terminal.app `do script` for terminal AND OMC (claude=interactive TTY, no headless Popen — Option-C deviation). RepoResolver: filesystem-first + difflib, prefers flat over org-nested dup. Code-reviewed (REQUEST_CHANGES → all findings fixed: false-success on non-zero exit, escaping, hyphen-parse, dispatch dead-end via new `SkillResult.handled`). **264 tests, ruff/pyright clean.** Safe live: parsed spec examples, osacompiled AppleScript clean, resolver vs real ~/gitstuff. Owner OS live-smoke pending. |
| — | Memory viewer | (07) | ✅ done | Offline local graph viewer: `MemoryStore.graph_snapshot()` + `edith/viewer/` (stdlib 127.0.0.1 server, vendored force-graph UMD, `--demo` seeder, `python -m edith.viewer`). **70 tests + 1 live-skipped, ruff/pyright clean.** Zero new runtime deps. Reads live Memory; repo ingestion populates it for real. |
| — | Repo ingestion | (08) | ✅ done | `edith/ingest/` populates the LIVE graph from local `patterninc` clones: discover→fetch→**REDACT (choke-point)**→Sonnet classify/Opus deep→map→`remember`. `python -m edith.ingest [--dry-run] [--repos] [--limit] [--data-dir] [--max-tokens]`, incremental skip on `Repo.last_commit_date`, secret-safe status report, one-time global `~/.claude/CLAUDE.md` owner context. Additive schema (`Repo` +4 cols, `Fact.source`, `authored_by` Repo→Person). **97 tests + 1 live-skipped, ruff/pyright clean.** Live smoke: 58 nodes to a temp dir, secret-scan clean. Full contributed-repos run is orchestrator-gated pending review. |
| — | NL finder + resolve-on-miss | (09) | ✅ done | `edith/finder/`: `find_repos` (model-free semantic+graph fuse → `relates_to` walk → rank by strength+degree; **Session 10:** per-token graph fallback fires when both signals score zero so a populated graph never silently returns nothing) + `summarize_hits` (Sonnet, injected); `python -m edith.finder "query"`. **Session 10:** ingest now writes via `VectorMemoryStore` so live Facts ARE embedded; `python -m edith.ingest --reembed` backfills existing graph-only Facts (local embedder, no model cost, idempotent). Live: 145 Facts reembedded, `finder "seo tools"` returns real repos. `resolve_repo` = HIT (graph `repo-<name>`) / RESOLVED (local clone or `gh` README → **REDACT choke-point** → fast Sonnet answer NOW + **background Opus** deep-extract coroutine the caller runs via `asyncio.create_task`, Slice-5 `think_async` seam) / NOT_FOUND (clean, no model). Thin Brain hook: recall-miss + repo mention + injected resolver → resolve then answer (**default `None` = no-op**, existing tests unchanged). Reuses ingest fetch/extract/graph_map. **110 tests + 1 live-skipped, ruff/pyright clean; planted-secret test proven non-vacuous.** Live smoke: `agentsmith` ingest (real Bifrost, relevance 0.72 Opus) → finder ranked it #1 with a real Sonnet summary; resolve HIT path no-model. |

| — | Workspace graph (multi-org) | (08 ext) | ✅ done | `edith/ingest/workspace.py` + `--workspace <org>`: metadata-graph a WHOLE GitHub org from the API (no clones, no model calls) — structural Repo node + embedded `gh_description` Fact each; deep extract stays on-demand. **Two workspaces, one graph:** Repo nodes carry `org`; ids org-scoped `repo-<org>-<name>` EXCEPT incumbent patterninc (`repo-<name>`, unprefixed, back-compat w/ resolve.py + existing nodes); fixes a real cross-org id collision. Additive/no-clobber (omit-empty summary/language; description in a distinct `gh_description` Fact). `Repo.org` column + guarded ALTER backfill. **LIVE graph now: 1378 repos (1297 patterninc + 81 ampmedia), 875 facts; 23 deep summaries preserved.** finder ranks across both orgs. 218 tests, ruff/pyright clean. PR #10. `scripts/clone_workspace.sh <org>` clones all active repos → `~/gitstuff/<org>/` (shallow, resumable) — sibling deliverable, decoupled from the graph. Archived skipped by default (`--include-archived`). |

Legend: ⬜ not started · 🚧 in progress · ✅ done · ⏸ blocked

> **Session 12 addendum — realtime resolve-on-miss now live in the daemon.** `edithd` wires
> `resolve_repo` into Brain (`_make_default_resolver` binds store+router for a real `MemoryStore`;
> injectable seam otherwise). Fixed a latent bug in `finder/resolve._gh_readme` (`--jq .content`
> combined with the `raw+json` Accept header → parsed markdown as JSON → every gh-path resolve was
> a spurious NOT_FOUND; only local-clone resolves ever worked). **Behavior now:** ask EDITH about a
> repo it doesn't know → live fetch + Sonnet answer NOW + background Opus deep-extract →
> `map_and_remember` **auto-adds it to the graph** → next mention is an instant HIT. Live-proven on
> `adczar` (graph 0→1 repos, accurate answer). 135 tests + 1 skipped, ruff/pyright clean.

## Next action

### ▶ SLICE 6 — Desktop control (START HERE — the LAST numbered slice)
Slices 0–5 DONE (+ Viewer / Ingest / NL-finder / Workspace-graph). **All PRs merged; master is clean.**
Next is **Slice 6** — read `docs/specs/06-desktop-control.md`. Voice-driven macOS automation shipped as
Skill(s): open apps (`open -a`), Spotify (`osascript`), open Terminal + `cd` to a repo + launch OMC
(spawn/own a shell). Owner: "launch a terminal in the concorde_lib repo and start OMC".

**Build steps (spec §Build steps, ordered):** scaffold the `desktop_control` Skill → repo scanner →
`CommandParser` (regex fast-path + haiku fallback → typed `DesktopAction`) → `RepoResolver`
(fuzzy name → path) → 4 executor fns (`launch_app` / `spotify_command` / `open_terminal_window` /
`spawn_shell_session`) → autonomy gate → bus wiring → smoke tests.

**Reuse, don't rebuild:** the Skill contract + Brain dispatch — copy the shape of `PRReviewSkill` /
`SessionQuerySkill` in **`edith/skills/`** (⚠ the spec says `src/skills/` — WRONG, it's `edith/skills/`).
`Router` `Tier.HAIKU` for the command-parse fallback; `VoiceIO.speak` for the spoken summary; the
graph for repo resolution. **RepoResolver MUST use the per-org clone dirs `~/gitstuff/patterninc/` AND
`~/gitstuff/ampmedia/`** (spec step-2 "walk ~/gitstuff/" is stale — clones are now in PER-ORG subdirs).
The live graph has 1378 org-tagged `Repo` nodes but their `path=""` (metadata pass didn't clone), so
resolve name → `~/gitstuff/<org>/<name>` on disk, or use the graph for the fuzzy match then map to disk.

**Gotchas the orchestrator MUST heed:**
- **Verify independently + do a LIVE run.** Tests green ≠ works (this project's recurring bite: the
  wake loop passed 161 tests but never woke live — `scores.get(path)` vs `max(scores.values())`).
- **Kuzu is single-process** → stop the viewer (`lsof -ti tcp:8765 | xargs kill`) before anything opens
  `memory.kuzu`.
- **Guard is still deferred** → each executor's `authorize()` is a seam defaulting to ALLOW (mirror
  `Narrator.budget_gate` / `Router.budget_check`); do NOT build Guard here.
- `open -a` / `osascript` / owned-shell are REAL OS side-effects → owner LIVE-SMOKE; unit-test with a
  mocked subprocess/osascript. Autonomy: AUTO for open/launch/cd/play; ASK for destructive.
- Bifrost creds in gitignored `.env` (`set -a; source .env; set +a`).

**Standing context for the new session:**
- **"Hey Edith" WORKS now.** Retrained model at `~/.edith/models/hey_edith.onnx` (0.767 synthetic vs
  the old 0.034; old backed up `.bak-recall042`). `.env EDITH_WAKE_MODEL` points at it. Run
  `python -m edith.voice --engine elevenlabs` → wakes, answers via Router (Sonnet), JARVIS persona
  (calls owner "sir"), half-duplex mic gate stops self-echo. SageMaker exec role `edith-sagemaker-exec-role`
  left in place for future retrains.
- **Workspace graph LIVE:** 1378 repos (1297 patterninc + 81 ampmedia), both orgs one graph, org-tagged,
  deep-extract on-demand. Clones at `~/gitstuff/{patterninc,ampmedia}` (1375, shallow). Graph backups at
  `~/.edith/data/*.bak-*`. `python -m edith.ingest --workspace <org>` (re-runnable, additive).
- **Highest-payoff NON-slice gap — daemon integration ("she talks back"):** there's still no composition
  root; EDITH only responds via the standalone `python -m edith.voice` harness, not a running `edithd`.
  Wiring VoiceIO→Brain→Router→speak into a launchable `edithd` (`python -m edith.daemon`) is the payoff
  that ties voice + router + session-awareness together. Consider before/after Slice 6.

**Deferred seams** (pick up when a slice needs them): **Guard** (`authorize`/budget); Router
`supervised_reason` + `think_async`/background-opus auto-escalation (the routing philosophy's centerpiece;
`resolve_tier` returns `suggest_background` but nothing acts on it); `Memory.compact()`.

**🔑 SECURITY — STILL OPEN:** rotate `ELEVENLABS_API_KEY` **and** `BIFROST_API_KEY` — both were exposed
in `.env`/chat and used live this session.

## Blockers / needs from owner

- ~~**Vector re-index decision (from Session 2).**~~ **RESOLVED + IMPLEMENTED (Session 3).**
  Adopted option (b): the vector layer is now **sqlite-vec** (Kuzu keeps the graph). Inserts
  are incremental — a Fact remembered after the store exists is recalled immediately, no
  rebuild. north-star Open Question #1's maturity caveat is now resolved with a working impl.
- ~~Bifrost `base_url` + API key (for Slice 1 Brain + Slice 5 Router).~~ **RESOLVED (Session 4):**
  in the gitignored `.env`; Router live smoke hit real Bifrost (200, non-empty). Key was pasted
  in chat 2026-07-06 → **rotate it in Bifrost** (noted in `.env`). Keychain retrieval = daemon work.
- ~~Establish `main` on GitHub.~~ **DONE (Session 11):** all branches consolidated into **`master`**
  (renamed from `main`), set as default, redundant branches deleted. Single-branch repo now.

## Known limitations

- **Kuzu embedded is single-process (Session 10).** The viewer, finder, and ingest each open
  `memory.kuzu` directly and contend on the on-disk file lock — only ONE may hold it at a time
  (running `--reembed` requires no other EDITH process on the DB). The production fix is routing
  ALL DB access through `edithd` (one owner of the handle; every other surface talks over the
  Control API). Noted, not built — out of scope for the embedding-gap fix.
