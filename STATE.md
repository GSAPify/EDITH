# EDITH — Build State

> Machine-and-human readable status. Update this at the end of every session (or at ~90% context).
> This is the first file a new session reads after `SESSION-PROTOCOL.md`.

**Current phase:** Slices 0–6 DONE + Viewer + Ingest + NL-finder + Workspace-graph + voice conversation mode + **daemon composition root ("she talks back", Session 18)** + **Guard / router-background seam / `Memory.compact()` (Session 19, PRs #17–#19 merged)**. **ALL NUMBERED SLICES BUILT + all three long-standing deferred seams closed.** **Session 20 (2026-07-27) then built all four operationalization items — launchd, Guard wiring, menu-bar, weekly refresh (PRs #24–#28, 408 passed together).** What remains is owner LIVE-SMOKE plus the follow-ups listed in §Next action (no SIGTERM handler; silence during refresh; ASK unreachable for desktop).
**Session 19 (2026-07-25) — three deferred seams merged.** `master` = `3131d33`. **PR #17 Guard** (`edith/guard/guard.py`: pure `authorize`/`Decision` + windowed token budget, north-star §6 — policy object only, **not yet injected into any subsystem**; every `authorize()`/`budget_check` seam still defaults to ALLOW). **PR #18 router background** (`edith/router/background.py`: `supervised_reason` draft→review, fully consumable; `think_async` free function = **seam only, no production consumer**). **PR #19 `Memory.compact()`** (bounded conv-Fact eviction in `store.py` + `vector.py`). **332 passed, 2 skipped on master; `ruff check .` has 3 pre-existing SIM115 in `scripts/sagemaker/train_hey_edith.py` (non-`edith/`, predates these merges) — `edith/` + `tests/` are clean.** **PR #20** (`feat/router-background-opus`, `BackgroundReasoner` + Brain triggers + daemon wiring — background opus WITH a real consumer) collided with #18 on `edith/router/background.py`, `edith/router/__init__.py`, `tests/test_router_background.py` (all "added/changed in both") plus a spec-number clash with `11-guard.md`. **RESOLVED 2026-07-25 (merge commit on the branch; PR #20 now MERGEABLE/CLEAN, 348 passed / 2 skipped):** #20's tracked, budget-gated `BackgroundReasoner.think_async` **supersedes** #18's free-function `think_async` (which was self-documented as "the seam, not the finished feature" — untracked, un-gated, no consumer); #18's `supervised_reason` **survives unchanged**, now typed against the shared `RouterLike` Protocol so both entry points use one seam. Its spec renumbered `11-background-reasoning.md` → **`13-background-reasoning.md`**, `12-router-background.md` carries a PARTIALLY-SUPERSEDED banner, and all 18 in-code "spec 11" references were renumbered to 13 (they would otherwise point at the Guard spec). **Merge order: #20 first, then this docs PR (#21).**
**Session 18 (2026-07-17) — daemon composition root** on branch `feat/daemon-composition-root` (spec 10, **PR #16, merged**). `python -m edith.daemon` boots the full daemon with a real `VoiceIO` on a shared bus → the graph-backed Brain answers voice with semantic recall + cross-session memory + all skills. Brain gained injectable `system_preamble`/`answer_max_tokens` + a **model-error seam** (catches `MODEL_CALL_ERRORS`, speaks an apology instead of going silent); daemon subscribes `brain.decision`→`voice.speak` (plain-answer path only, no double-speak); shared `edith/voice/persona.py`; injected `bus`. Tightened both `MemoryLike` protocols' `remember(edges)` to `list[Edge]` so a real `VectorMemoryStore` type-checks into the daemon. **302 tests, ruff + pyright clean.** `__main__` + live loop are owner-smoke; mic shutdown is Ctrl-C/process-exit (to_thread loop doesn't cancel). See `docs/specs/10-daemon-composition.md` §Completion Record.
**Active slice:** → all numbered slices done + voice conversation mode done. Session 16 (2026-07-12): built **Slice 6 (Desktop control)** advisor-first + TDD on branch `feat/slice-6-desktop-control` (**PR #13, merged**) — `edith/desktop/` (regex `parse_command` + filesystem `RepoResolver` + osascript/`open` executors behind an injected `Runner` seam) + `DesktopControlSkill` registered LAST in edithd's Brain. 264 tests + 2 skipped, ruff/pyright clean (code-reviewer REQUEST_CHANGES round folded in: return-code error path, Spotify escaping, hyphenated-repo parse, and a new `SkillResult.handled` flag so a broad-trigger skill can decline a turn and Brain falls through to the answer loop instead of dead-ending). Terminal.app `do script` drives BOTH the visible-terminal and the OMC-launch paths (claude is an interactive TTY REPL, won't run headless under `Popen` — spec Option-C deviation). `RepoResolver` prefers the flat `~/gitstuff/<name>` over the org-nested duplicate (bulk-clone artifact, verified identical remote); same-depth collisions still ASK. Safe live checks: parsed all spec examples, `osacompile`d the AppleScript clean, resolver ran against the real 1400-repo tree. **Owner LIVE-SMOKE of actual OS actions (Spotify/Terminal/OMC) still pending.** Prior that session: multi-org workspace graph (PR #10), voice self-echo/persona fixes (PRs #8/#12), SageMaker `hey_edith` retrain, Router Slice 5 (PR #7).
**Session 17 (2026-07-14) — voice conversation mode** on branch `feat/voice-conversation-mode` (PR #14, merged). Built by a 3-agent team + lead integration: `TurnBuffer` (in-session recent-turns memory) spliced into the voice harness via pure `build_messages`; `ConversationWindow` (follow-up window, no re-wake) + `Endpointer` (silence endpointing, no cut-off on pauses) wired into `live.py`; `m`+enter mute via `set_paused`. Brain's `history` splice built + tested as the seam for the DEFERRED next task: **route voice → Brain + real Kuzu in the `edithd` composition root** (semantic recall + cross-session remember — the "she talks back" daemon gap). Live-audio path is owner-smoke; decision logic unit-tested. See `docs/specs/voice-conversation-mode.md` §Build record. **Also found stranded off master:** commit `458f77f` (long-reply max_tokens=120 fix) never reached master via PR #12 — flag for its own recovery PR.
**Repo:** everything lands on **`master`** (renamed from `main`, Session 11); `master` is the GitHub
default. New work = branch off `master`, PR in. **Branch hygiene is stale:** ~10 already-merged
remote branches (`build/slice-3-voice`, `docs/readme-refresh`, `feat/daemon-composition-root`,
`fix/voice-*`, …) were never deleted, plus 3 live agent worktrees under `.claude/worktrees/` —
`git branch -vv` is misleading; trust `gh pr list` + `origin/master`.
**Live graph** at `~/.edith/data/memory.kuzu` (31 MB, last write **2026-07-12**): **2291 nodes —
1378 Repo (1297 patterninc + 81 ampmedia), 875 Fact, 26 Person, 12 Project**, embedded, no leak.
(The old "206 nodes / 23 Repo / 145 Fact" figure here predated the workspace-graph pass — corrected.)
**Prev session:** 2026-07-07 — Session 12 (built Slice 2 PR-review skill, TDD, delegated to Opus executor then verified independently + live-smoked). Added the Skill contract + Brain trigger-dispatch registry (didn't exist before — Brain went straight to model), `PRReviewSkill` 7-step flow, injectable async `gh` runner, and `Person.gh_handle` via a guarded non-destructive migration. Confirm-gate is the crux and is proven unreachable-on-deny by both a non-vacuous test and a live smoke (real gh + real Opus on `patterninc/agents#2423` → real review, `posted=False`, zero `pr review` writes). 130 tests + 1 skipped, ruff/pyright clean. Session 10 (prior): closed the ingest↔finder embedding gap, TDD. Ingest now writes via `VectorMemoryStore` so Facts are embedded on `remember` (Fix 1, `run_ingest(embedder=…)` seam); `VectorMemoryStore.backfill_embeddings()` + `python -m edith.ingest --reembed` embed existing graph-only Facts with the LOCAL embedder, no model calls, idempotent, credential-free (Fix 2); `find_repos` adds a per-token graph fallback that fires ONLY when both signals score zero, so a populated graph never silently returns nothing (Fix 3). 114 tests green, ruff/pyright clean. Live: `--reembed` embedded the 145 real Facts; `python -m edith.finder "seo tools"` now returns real repos (was "No repos matched"). Known limitation documented: Kuzu embedded is single-process (lock contention across viewer/finder/ingest; prod fix = route all DB access through `edithd`).

## Slice status

| # | Slice | Spec | Build | Notes |
|---|-------|------|-------|-------|
| 0 | North-star architecture | ✅ done | — | Authoritative doc |
| 1 | Memory + Brain | ✅ done | ✅ core done | Memory (Kuzu graph + sqlite-vec) + **bus** + **Router** (Bifrost, live-smoke green) + **Brain loop** + **edithd daemon** (unix-socket Control API pause/resume/kill/status, 0600, startup/shutdown ordering, pause-suspends-Memory, launchd plist template) — **59 tests + 1 live-skipped, ruff/pyright clean, 3-reviewer validated**. Documented seams left for their slices: `compact()`, Guard (budget/authorize), encrypted-volume mount, VoiceIO/SessionBus wiring. |
| 2 | PR-review skill | ✅ done | ✅ done | `edith/skills/` (Skill contract + `gh` runner + `PRReviewSkill`) + Brain trigger-dispatch + `Person.gh_handle` (guarded migration) + **registered in edithd's Brain** (`skills=[PRReviewSkill(router)]`; default `_silent`/`_deny` → dispatches but never posts until Slice 3 voice). Confirm-gate is the crux: `gh pr review` unreachable unless `confirm()==True`, default DENY. Diff redacted (known shapes) before Opus. **131 tests + 1 skipped, ruff/pyright clean.** LIVE-smoked: real gh + real Opus on `patterninc/agents#2423`, confirm=deny → real review, `posted=False`, zero `pr review` writes. **Known gap (verified live):** resolution against the real graph currently ALWAYS asks (ingested Persons have `gh_handle=""`; recall surfaces no Repo for a person name) — safe ask-when-unsure path; "instant HIT next time" is only partial. Diff-size cost gate NOT wired. See spec 02 §Follow-ups. |
| 3 | Voice | ✅ done | ✅ done | `edith/voice/`: TTSAdapter ABC + ElevenLabs/Piper adapters + VoiceIO (speak→redact→cap, wake/utterance bus events, pause-suppress, barge-in) + edithd wiring (speak→PRReviewSkill; pause/resume→set_paused via new ControlServer on_pause/on_resume callbacks) + CLI harnesses. Hardware/ML behind injectable seams → **161 tests + 1 skipped, ruff/pyright clean, zero type:ignore.** Built by OMC tmux team (3 workers). **Wiring seam is in place + unit-tested; the daemon does NOT speak yet** — every real path builds `EdithDaemon(voice=None)` (no composition root constructs a real `VoiceIO`, and mic/wake/STT/playback are stub seams). Audio path = owner LIVE-SMOKE. **Session 13 follow-up:** `[voice]` extra installed, `sanitize_text` broadened for the ElevenLabs egress (DONE), ElevenLabs adapter fixed to v2.56 API, and mic/wake/STT seam bodies implemented in `edith/voice/live.py` + `python -m edith.voice` (openWakeWord hey_jarvis + whisper small.en + sounddevice) — doc-derived, NOT hardware-verified. Owner runs the real audio smoke (mic/speaker/key). |
| 4 | Session awareness | ✅ done | ✅ done | `edith/session/`: **spike** (transcript-tail confirmed on the live machine — see `scratch/spike_session_tap_findings.md`) → **TranscriptCollector** (dep-free EOF-seek poller of `~/.claude/projects/**/*.jsonl`; primes to EOF so history is NOT replayed) → **SessionBus** (normalize→classify→**REDACT choke-point**→`session.event`/`session.state` + in-mem states map + Control API `last_event`) → **Narrator** (3-class policy: silent / spoken-local template / model-gated haiku; idle via `tick()`) → **SessionQuerySkill** ("what is session 2 doing?" via Brain dispatch, phrase triggers). edithd wires it all (`enable_session_awareness` flag gates the live tail off in tests). Hardened `sanitize_text` with a **connection-URI password** pattern (the killer-demo leak). **35 new tests (196 total +1 skipped), ruff/pyright clean.** LIVE-smoked on real transcripts: 11.7k events classified, real pasted Snowflake/Postgres creds → `[REDACTED]`, 0 leaks. **Cost gate (spec #5): per-session error-narration cooldown** — measured 452→72 model calls over the real stream (~0.6/session); Guard's real budget still deferred. Deviations (documented): Narrator is a collaborator (not in Brain); collector polls (not watchdog). |
| 5 | Router | ✅ done | ✅ done | `edith/router/`: `tiers.py` (`resolve_tier` + `TaskType`; owns the `Tier` enum now) — latency-first policy (Sonnet=live voice, Haiku=acks, Opus=explicit/background), override rules (ACK_FILLER→Haiku, HAIKU→Sonnet on size, OPUS budget-gated→Sonnet+`budget_limited`, deep signal→Sonnet+`suggest_background`). `bifrost.py`: `model_call_stream` (Anthropic SSE→`ModelChunk`), **`model_call_masked`** (tier-parameterized, answer defaults SONNET not opus; TRUE overlap — both requests fire before draining), `budget_check`+`redactor` seams, **redaction choke-point inside every `model_call*`**. Non-streaming POST unchanged (callers untouched). **17 new tests (212 total +1 skipped), ruff/pyright clean.** LIVE-smoked: `model_call_stream` vs REAL Bifrost yielded real tokens (SSE parser verified against actual stream). **Session 19 update (PR #18):** `edith/router/background.py` now ships `supervised_reason` (draft Sonnet → Opus critique+improve, awaited, consumable) + a `think_async` free function. **Still UNMET:** `think_async` has **no production consumer** (nothing speaks a ~20 s-late answer through the voice half-duplex gate / cooldown / conversation-window), and `resolve_tier`'s `suggest_background` flag is still returned-but-unacted — that's exactly what open **PR #20** builds (and why it's worth resolving rather than closing). Masking still has no live consumer (needs VoiceIO `speak_stream`); OpenAI provider-swap config-only. |
| 6 | Desktop control | ✅ done | ✅ done | `edith/desktop/` (parser + `RepoResolver` + executors behind an injected `Runner` seam) + `DesktopControlSkill` (registered LAST in edithd, `needs_confirmation=False`, AUTO-only). "open Spotify", "play X on Spotify", "pause/skip/volume", "open a terminal in <repo>", "start OMC in <repo>". Terminal.app `do script` for terminal AND OMC (claude=interactive TTY, no headless Popen — Option-C deviation). RepoResolver: filesystem-first + difflib, prefers flat over org-nested dup. Code-reviewed (REQUEST_CHANGES → all findings fixed: false-success on non-zero exit, escaping, hyphen-parse, dispatch dead-end via new `SkillResult.handled`). **264 tests, ruff/pyright clean.** Safe live: parsed spec examples, osacompiled AppleScript clean, resolver vs real ~/gitstuff. Owner OS live-smoke pending. |
| — | Guard (autonomy + budget) | (11) | ✅ built + wired + default-deny | PR #17. `edith/guard/guard.py`: pure policy object — `authorize(action, needs_confirmation) → Decision{ALLOW,ASK,DENY}` (denylist) + windowed token budget (`token_budget`, `window_seconds`, injected `clock`). No I/O, no model calls, no bus → trivially testable, construct once + inject. Does NOT duplicate redaction (§6.1 stays with `sanitize_text` at the Router choke-point). **134 test lines / suite green.** ~~⚠ Nobody constructs it yet~~ **WIRED in Session 20 (PR #27):** one Guard per daemon in the composition root → `Router.budget_check`, `BackgroundReasoner`, `Narrator.budget_gate` (adapted — it's a no-arg `Callable[[], bool]`, bound to `Tier.HAIKU`), `control.py`'s `BudgetView` (so the menu bar shows real usage), **plus a NEW gate call site in `DesktopControlSkill`** — there was never an `authorize()` seam in `edith/desktop/`, contrary to what this file used to say. The charge path is the crux and it is closed: a new `on_usage` seam on `Router` calls `Guard.record` from `model_call` and `model_call_stream`; `model_call_masked` delegates to both so it is charged twice for its two billing events and **must not** get a third call site. **Safety property, verified: `budget_check` is only ever called with `Tier.OPUS` (`bifrost.py:229`, `background.py:156`) plus HAIKU for narration — nothing gates the live conversational path, so exhaustion downgrades opus→Sonnet and drops narration to a local template but EDITH cannot go mute.** **DEFAULT-DENY since PR #40:** `daemon/__main__.py` builds the one Guard with `allowlist=frozenset(intent.value for intent in Intent)`, flipping the posture from "allow unless denylisted" to "deny unless vetted" — the right shape for a 4-value vocabulary, since a denylist only ever covers verbs someone thought to add. Precedence in `authorize`: denylist DENY wins, then allowlist DENY, then `needs_confirmation` ASK, else ALLOW. Still true: the gate matches the intent **verb**, never the argument, and ASK is unreachable (see §Next action). |
| — | `Memory.compact()` | (01 seam) | ✅ done | PR #19. Bounded conversation-Fact eviction across `edith/memory/store.py` + `vector.py` (graph rows AND their sqlite-vec embeddings, so compaction can't leave orphaned vectors). Closes the oldest seam in the repo (deferred since Slice 1). Not yet called on a schedule by the daemon. |
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

### How to start EDITH today (the real, current answer)

```bash
cd ~/gitstuff/EDITH
source .venv/bin/activate
set -a; source .env; set +a            # BIFROST_* + ELEVENLABS_* + EDITH_WAKE_MODEL
lsof -ti tcp:8765 | xargs kill 2>/dev/null   # Kuzu is single-process: free the graph first
python -m edith.daemon --engine elevenlabs   # full daemon: voice → Brain → live graph → speak
```
`python -m edith.daemon` (spec 10) is the **composition root** — real `VoiceIO` + the graph-backed
`VectorMemoryStore` + `Router` + all skills on one bus. Say "Hey Edith, …". Ctrl-C stops it.
Single subsystems still run standalone: `python -m edith.voice` (voice only, no daemon/session tap),
`python -m edith.viewer`, `python -m edith.finder "…"`, `python -m edith.session`,
`python -m edith.ingest`. **Only ONE of these may hold `memory.kuzu` at a time.**

### ▶ NEXT — after Session 20, all four operational items are BUILT (PRs #24–#28)

**Session 20 (2026-07-27) shipped all four**, fanned out to parallel agents in isolated worktrees.
Verified merged together: **408 passed, 2 skipped**, `ruff check edith tests` clean.
`348 base + 13 (#24) + 23 (#25) + 0 (#26, refactor) + 13 (#27) + 11 (#28) = 408.`

| PR | Item | Owner LIVE-SMOKE still needed |
|----|------|-------------------------------|
| #24 | launchd LaunchAgent (`deploy/`) | `launchctl bootstrap`, survives logout/login |
| #25 | menu-bar app (`edith/menubar/`) | rendering, `rumps.timer` poll, confirm dialog |
| #26 | recovered stranded `compact()` review fix | — |
| #27 | Guard wired into all five gates | budget behaviour in a real session |
| #28 | weekly refresh scheduled in-process | a real ~1.3-min pass against the live graph |

**⚠ Merge caveat:** #27 and #28 both add an import at the same line of `edith/daemon/edithd.py`.
Whichever merges **second** hits a two-line conflict — **keep both imports**. Verified: the resolved
combination is the 408-passing tree above. Not a design conflict.

**Two corrections to what this file previously claimed** (both were mine, both verified against code):

1. ~~"1297 repos of GitHub API calls per org per week — respect `X-RateLimit-Remaining`"~~ **WRONG.**
   `_gh_list_repos` (`workspace.py:113`) is one `gh api orgs/<org>/repos?per_page=100 --paginate` —
   **~13 calls for patterninc, 1 for ampmedia**, against 5000/hr. Rate limiting is a non-issue and
   no throttling was built. The real cost is ~1300 serial Kuzu writes, **benchmarked at 54 ms/repo
   → ~1.3 min for 1378 repos**. `tenacity` still went on that `subprocess.run(check=True)` — for
   *flakiness*, not rate limits. The `last_commit_date` skip is therefore a **lock-duration**
   optimisation, not a cost control (still not built — its own PR).
2. ~~"every `edith/desktop` executor's `authorize()` defaults to ALLOW"~~ **There was no such seam.**
   `grep -rn "authorize\|Decision" edith/desktop/` was empty; `DesktopControlSkill` ran every parsed
   OS action ungated. #27 **added** the call site rather than injecting into one.

**The Kuzu lock contract** (verified by code-read in #24, with line citations in
`docs/specs/01-memory-brain.md`) — this is what decided #28's design:
- **`pause` does NOT release the handle.** `RuntimeState.pause()` (`state.py:48-51`) flips an enum
  and never touches `self._memory`.
- Released only by `MemoryStore.close()` (`store.py:391-394`) ← only `EdithDaemon.stop()`
  (`edithd.py:361-402`) ← only a Control API `kill` or Ctrl-C.
- Under launchd `KeepAlive`, `kill` → instant respawn → lock re-taken. **`launchctl bootout` is the
  only way to free the graph** for viewer/finder/ingest. That is a real usability regression of
  going always-on, documented in `deploy/README.md`.
- So "stop the daemon around a scheduled ingest" loses, and #28 schedules it **in-process**.

**Still open after Session 20** (each its own item, none smuggled into the PRs above):
- **No `SIGTERM`/`SIGINT` handler** (`grep -rn "add_signal_handler\|signal\." edith/` is empty), so
  `launchctl bootout` is an *ungraceful* stop — `compact()` and `close()` never run. Wants a signal
  handler plus a `KeepAlive` policy distinguishing intentional stop from crash.
- **During a refresh EDITH ignores turns silently** — #28 folds the refresh flag into `is_paused`
  rather than taking a cross-thread lock, so a live turn is skipped entirely (no reply, not even an
  apology) for ~1.3 min/week. Deliberate (avoids a new deadlock class); revisit if it grates.
- **`BackgroundReasoner.on_done` and `finder/resolve.py::_deep_extract`** write the same Memory
  handle without checking `is_paused`, so they can still race #28's worker thread.
- **ASK is unreachable for desktop control.** #27 maps ASK→DENY fail-closed and #40 made the gate
  default-deny over the four intents, but nothing sets `needs_confirmation=True` on that skill, so
  the *middle* verdict still cannot happen. A real voice-confirm
  ("should I?" → listen) is what would make ASK mean something — `PRReviewSkill`'s `Confirm`
  callable defaults to `_deny` and the daemon wires that default, so no such path exists anywhere.
- `last_commit_date` incremental skip; routing all Kuzu access through `edithd`.

### ⚠ Catch-up since PR #29 (mechanical — not a real session close)

This file was last given a real session update at PR #29 (`docs/state-session20`). Six PRs have
since merged to `master` without a matching STATE.md entry. Listed here from `git log` only —
no live-smoke or verification detail is claimed, because none was captured. A real session should
fold these into the sections above with that detail, then delete this subsection.

| PR | Branch | Commit | What |
|----|--------|--------|------|
| #30 | `docs/readme-deemoji` | `ff254ce` | README de-emoji + refresh for #24–#28 |
| #31 | `fix/menubar-standalone` | `a73ae45` | menu bar stopped importing the daemon; unhangs the suite once `rumps` is installed |
| #32 | `fix/menubar-visible` | `f69e916` | menu-bar status item actually renders (owner-confirmed visible) |
| #33 | `docs/spec-14-log-redaction` | `0843fe1` | spec 14 — log-redaction research (no code; recommendation only, see §Known limitations) |
| #34 | `fix/ingest-error-containment` | `811f240` | `run_ingest` gathers with `return_exceptions=True` + records per-repo, so one failing repo no longer discards the whole run's writes |
| #35 | `fix/daemon-quiet-flag` | `ef2a7f0` | `--no-session-narration` CLI flag — session narration blinds wake detection with several active Claude Code sessions; default unchanged (narration on) |

PR #36 (`docs/readme-test-count-and-narration-flag`, merged) documented #35's flag in the README
and fixed the test count it left stale (410 → 416). This STATE.md catch-up is its own follow-up PR.

### ⚠ Catch-up part 2 — PRs #38–#52 (also mechanical)

Same caveat as above: read off `git log` + `gh pr list`, no verification detail claimed beyond the
suite. **Master is now `aac4ebc` = 507 passed, 2 skipped, `ruff check edith tests` clean.**
(#36/#37 are the two docs PRs already described above.)

| PR | Branch | Commit | What |
|----|--------|--------|------|
| #38 | `fix/desktop-spotify-bad-classifier-reply` | `0069e04` | a bad classifier reply raised `ValueError` out of `Brain._dispatch_skill` and killed the whole turn — caught now. Was filed-not-built in §Security review |
| #39 | `fix/secrets-repr-leaks-key` | `c3e641d` | `Secrets.__repr__` printed the API key. `field(repr=False)`, as filed |
| #40 | `feat/guard-default-deny-desktop-vocabulary` | `9ed9462` | **`Guard.authorize` is default-deny now.** The composition root passes an `allowlist` of the four `Intent` values, so anything outside `{open_app, spotify, terminal, omc_launch}` is DENY. This is the "make it default-deny over its closed enum" item, built |
| #41 | `feat/daemon-show-transcript` | `1ee88cc` | `--show-transcript` echoes heard utterances + spoken answers. Off by default: under launchd stdout is the unrotated, unredacted `edithd.out.log` |
| #42–#48 | rolled up as **#49** | `03b2009` | the voice-fix stack, landed together: `EDITH_WAKE_MODEL` actually honored instead of listening for `hey_jarvis` (#42); tiers moved to **opus-5 / sonnet-5 behind one shared model map** (#43); conversation window held 3 exchanges, not the 6 it claimed (#44); **prompt caching**, with the cached prefix charged to the budget (#45); mic thread stops cooperatively so Ctrl-C stopped segfaulting (#46); `--preflight` provokes every macOS permission prompt then reports (#47); mic pre-roll so a word's quiet onset is not clipped (#48) |
| #50 | `feat/aec-bench-foundation` | `5e0c257` | headless echo-cancellation bench: deterministic stimulus, ERLE/double-talk/latency metrics, `FakeDuplex`, runner that archives audio |
| #51 | `feat/duplex-vpio-backend` | `c1f0195` | `VpioDuplex` — macOS Voice Processing I/O (Apple's own canceller) behind the duplex seam |
| #52 | `feat/duplex-aec3-backend` | `d3c5ac4` | `SpeexEchoCanceller` as the software arm. **Core only — there is no `DuplexAudio` backend for it yet.** WebRTC AEC3 was the intended comparison and cannot be built here (see the `pyproject.toml` note) |

**Open as of `aac4ebc`** (check `gh pr list` before trusting this — either may have merged since):
**#54** (`feat/aec-bench-cli`) makes the bench runnable and fixes three
defects the first real-hardware run exposed — measured **ERLE 2.1 → 7.8 dB**; `added_latency_ms` is
documented known-bad (reports playback queue depth). **#55** is the matching README catch-up.
With #54 in the tree the suite is **515 passed, 2 skipped**.

**Why the duplex work exists:** it is the real fix for what `--no-session-narration` (#35) works
around — the half-duplex mic gate means EDITH cannot hear the wake word while she speaks. It is a
**spike**: nothing in the voice path consumes any of it yet.

---

#### Historical — the four items as they stood before Session 20 (kept for context)

All numbered slices are built and all three long-standing seams merged (#17/#18/#19). The gap between
"the code works" and "EDITH is the always-on presence in `00-north-star.md`" was **four operational
items, none of which is a new slice.** Ordered by payoff:

**1. Always-on `edithd` (launchd).** Today EDITH only exists while a terminal holds
`python -m edith.daemon` in the foreground. **`deploy/com.gsapify.edithd.plist` does NOT exist** —
spec 01 §Deliverables (line ~798) claims a launchd template shipped and STATE.md repeated the claim;
there is no `.plist` anywhere in the repo or in git history. Write it (`RunAtLoad` + `KeepAlive`,
`StandardOutPath`/`StandardErrorPath`, env via `EnvironmentVariables` or a wrapper that sources
`.env`), `launchctl bootstrap gui/$UID`, verify it survives logout/login. Until then "always-on" is
aspirational.

**2. Scheduled graph refresh — nothing exists.** There is **no cron, no launchd job, no scheduler,
and no `--since`/incremental-refresh entry point**; the graph's last write was **2026-07-12**. The
pieces are all there and re-runnable/additive — a weekly refresh is a `launchd`
`StartCalendarInterval` job (or a `LaunchAgent` per org) wrapping what already works:
```bash
python -m edith.ingest --workspace patterninc      # metadata pass, no clones, no model calls
python -m edith.ingest --workspace ampmedia
python -m edith.ingest --reembed                   # embed graph-only Facts (local, free)
scripts/clone_workspace.sh patterninc              # optional: refresh shallow clones
```
Design notes before building it (verified against the code, not assumed):
- **The `--workspace` metadata pass has NO incremental skip.** `edith/ingest/workspace.py` *writes*
  `Repo.last_commit_date` (from the API's `pushed_at`) but never reads it to skip — its only skip is
  `skipped_archived`. Every weekly run re-walks all 1297 repos. The clone-based ingest path is the
  one with `last_commit_date` skip logic. Add the skip to `workspace.py`, or accept full re-walks.
- **Model-free is not request-free.** 1297 repos of GitHub API calls per org per week — page it,
  respect `X-RateLimit-Remaining`, and use `tenacity` for the 403/secondary-limit backoff rather
  than a hand-rolled sleep loop. This shapes whether the job is one pass or throttled/chunked.
- **Deep extract is NOT free** (Opus per repo) — gate any scheduled deep pass on Guard's budget and
  do not schedule it across 1297 repos. The weekly job should be metadata + `--reembed` only.
- **It must not run while `edithd` holds the Kuzu handle** (single-process) → either stop/start the
  daemon around it, or route ingest through the Control API. That contention is the real decision.

**3. Wire Guard in (it's built but injected nowhere).** `Narrator.budget_gate`,
`Router.budget_check`, and every `edith/desktop` executor `authorize()` still default to ALLOW.
One `Guard` constructed in the composition root and passed to those seams closes north-star §6.
This is also the throttle any background-opus work (PR #20) and any scheduled deep-extract needs.

**4. Menu-bar control — not built.** north-star §1/§4.2 make it the only visible surface
(pause/resume/kill + a status label). The Control API side exists (`edith/daemon/control.py`,
unix socket, 0600) and `edith/daemon/client.py` can drive it; there is **no** menu-bar app (no
`rumps`/`pystray`/`NSStatusBar` anywhere). Today pause/resume/kill = the CLI client only.

**Also pending, smaller:** ~~resolve PR #20~~ **DONE** (see the Session 19 line — superseded #18's
free-function `think_async`, kept its `supervised_reason`, spec → 13; **merge #20 before this docs
PR**). Delete the ~10 merged remote branches. Refresh the stale `README.md`.
Owner LIVE-SMOKE still outstanding for: desktop-control OS actions (Spotify/Terminal/OMC), the
full `edith.daemon` voice loop, and background reasoning ("think about X" → opus pings back by voice).

**Gotchas any new session MUST heed:**
- **Verify independently + do a LIVE run.** Tests green ≠ works (this project's recurring bite: the
  wake loop passed 161 tests but never woke live — `scores.get(path)` vs `max(scores.values())`).
- **Kuzu is single-process** → stop the viewer (`lsof -ti tcp:8765 | xargs kill`) and the daemon
  before anything else opens `memory.kuzu`.
- Bifrost creds in gitignored `.env` (`set -a; source .env; set +a`).
- `open -a` / `osascript` / owned-shell are REAL OS side-effects → owner LIVE-SMOKE; unit-test with a
  mocked subprocess/osascript.
- `ruff check .` reports 3 pre-existing SIM115 in `scripts/sagemaker/train_hey_edith.py`. That is the
  baseline — scope lint to `edith/ tests/` when checking your own work, and don't "fix" it inside an
  unrelated PR.

**Standing context for the new session:**
- **"Hey Edith" WORKS now.** Retrained model at `~/.edith/models/hey_edith.onnx` (0.767 synthetic vs
  the old 0.034; old backed up `.bak-recall042`). `.env EDITH_WAKE_MODEL` points at it. Run
  `python -m edith.voice --engine elevenlabs` → wakes, answers via Router (Sonnet), JARVIS persona
  (calls owner "sir"), half-duplex mic gate stops self-echo. SageMaker exec role `edith-sagemaker-exec-role`
  left in place for future retrains.
- **Workspace graph LIVE:** 1378 repos (1297 patterninc + 81 ampmedia), both orgs one graph, org-tagged,
  deep-extract on-demand. Clones at `~/gitstuff/{patterninc,ampmedia}` (1375, shallow). Graph backups at
  `~/.edith/data/*.bak-*`. `python -m edith.ingest --workspace <org>` (re-runnable, additive).
- ~~**Highest-payoff NON-slice gap — daemon integration ("she talks back").**~~ **DONE (Session 18,
  PR #16):** `python -m edith.daemon` is the composition root — VoiceIO→Brain→Router→graph→speak in one
  process. The remaining gap is not integration, it is **lifecycle**: nothing starts it but you (item 1
  above).

**Deferred seams** — the three that stood since Slice 1 are now **CLOSED**: ~~Guard~~ (#17, built —
but see §Next action item 3, still injected nowhere), ~~Router `supervised_reason`~~ (#18, built +
consumable), ~~`Memory.compact()`~~ (#19, built — not yet called on any schedule). **Still open:**
`think_async` with a production consumer (PR #20 — built + conflict resolved, awaiting merge + owner
live-smoke); `resolve_tier`'s `suggest_background`
returned-but-unacted; `model_call_masked` has no live consumer (needs VoiceIO `speak_stream`);
encrypted-volume mount; routing ALL Kuzu access through `edithd` (the single-process fix).

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

## Security review (Session 20) — 2 HIGH found, both addressed

- **🔴 LIVE credential exposure — FIXED on the machine, and now enforced.** `.env` was `0644`
  world-readable, `/Users/akhilsingh` is group-`staff` traversable, and a second local account
  (**uid 501 `pattern`**) could read `BIFROST_API_KEY` + `ELEVENLABS_API_KEY` outright, no
  escalation. Fixed immediately (`chmod 600 .env`, `chmod 700 ~/.edith`) and durably in PR #24:
  `edithd-launcher.sh` now **refuses to boot** unless `.env` is mode 600 and owned by the
  invoking user, so a chmod regression fails loudly instead of silently re-exposing the key.
  Enforcing beats documenting — the old runbook said only "never commit it", and *permissions*
  were what actually mattered. **⚠ OWNER DECISION: rotate both keys** if that account has ever
  been reachable by anyone else. (This is the third standing reason to rotate — see §Blockers.)
- ~~**🔴 Guard's autonomy gate is INERT in every shipped config.**~~ **FIXED by PR #40** — the
  composition root now builds the Guard with an `allowlist` of the four `Intent` values, so
  `authorize()` DENIES anything outside the closed desktop vocabulary instead of allowing
  everything. **Two caveats survive:** Guard matches the intent **verb**, never the argument (so
  argument safety stays with the executors — that is where bundle paths are refused), and **ASK is
  still unreachable** because `needs_confirmation` is `False` on `DesktopControlSkill` and
  `PRReviewSkill`'s `Confirm` defaults to `_deny`. Also note `EdithDaemon` still falls back to a
  bare `Guard()` (denylist-only, inert) when none is injected — the test/embedding path, not what
  `python -m edith.daemon` ships. **The original finding is kept verbatim in the bullet below**, both
  because it records how the hole was found empirically and because its bundle-path note still holds.
- **🔴 Guard's autonomy gate is INERT in every shipped config.** Verified empirically, not
  inferred: `Intent` values are `{open_app, spotify, terminal, omc_launch}`, `_DEFAULT_DENYLIST`
  is `{rm_rf, drop_table, force_push, shutdown, disk_wipe}` — **intersection EMPTY** — and
  `needs_confirmation` is a `False` class constant, so `authorize()` provably returns ALLOW for
  100% of desktop actions. `daemon/__main__.py` builds a bare `Guard()`. **Not a regression**
  (there was no gate before #27), but do NOT read "Guard is wired" as "OS actions are gated".
  The reachable exploit — `"open /tmp/evil.app"` → `open -a` executing an attacker-planted
  bundle, reachable by anyone within earshot — **is fixed** in #27 by refusing bundle *paths*
  (`"open Slack"` still works). Guard cannot catch that class at any denylist setting: it
  matches the intent **verb**, never the argument.
- ~~**Filed, not built**~~ **ALL THREE NOW BUILT**, each as its own PR as planned:
  `Guard.authorize` default-deny over its closed enum (**#40**); the `spotify_cmd` `ValueError`
  from a bad classifier reply that escaped `Brain._dispatch_skill` and killed the turn (**#38**);
  `Secrets.__repr__` exposing the key (**#39**).
- **Logs are NOT redacted and NOT rotated.** `sanitize_text` is the choke-point for model/TTS/bus
  payloads; it does not run on log records. Python's `lastResort` handler sends WARNING+ to
  stderr — e.g. `TranscriptCollector` poll errors derived from tailing `~/.claude/projects` —
  and PR #24's plist captures that **permanently**. Keep `~/.edith` at `700`; add a
  `newsyslog.d` entry if unbounded growth matters.
- **Verified clean:** no credential can reach the logs (`_amain`'s failed-creds warning prints
  variable *names*; the key lives only in an `x-api-key` header and Python tracebacks don't
  render locals); the desktop gate is correctly placed and **unbypassable** (both the regex and
  haiku-classifier paths hit `_authorize` before every executor); `on_usage` carries two ints so
  it cannot bypass redaction; AppleScript escaping is correct; SQL/Cypher are parameterized; no
  `shell=True` anywhere. **#25 and #26 had no security findings.**

## Known limitations

- ~~**EDITH is not actually always-on (Session 19).**~~ **ADDRESSED Session 20 (PR #24)** — the
  LaunchAgent template, env wrapper and runbook now exist in `deploy/`. Still true until the owner
  runs `launchctl bootstrap`: **that step is unverified**, and nothing here proves the daemon starts
  correctly under real launchd. Most likely first-run failure, with its log signature documented:
  `python -m edith.daemon` always builds a real `VoiceIO` and exits 1 without the `[voice]` extra,
  which under `KeepAlive` becomes a 10-second respawn loop in `~/.edith/logs/edithd.err.log`.
- ~~**The graph does not refresh itself (Session 19).**~~ **ADDRESSED Session 20 (PR #28)** — the
  daemon now schedules the model-free passes in-process, **off by default** behind
  `enable_graph_refresh`. Never deep-extracts. Still unverified against the live graph, and the
  graph is still at its 2026-07-12 write until someone enables it.
- ~~**Guard exists but gates nothing (Session 19).**~~ **ADDRESSED Session 20 (PR #27)** — see the
  Guard row in the slice table. **PR #40 closed the caveat that used to live here** —
  the gate is default-deny over the desktop vocabulary now, not denylist-only. What survives is
  narrower: **ASK is unreachable** (nothing sets `needs_confirmation=True` on that skill), and the
  gate matches the intent verb, never its argument.
- **A stranded-commit pattern has now bitten twice.** Session 17 lost `458f77f`; Session 20 found
  `2227b73` (a `compact()` review fix — a broad `except Exception` around a sqlite prune, and a
  duplicated selection query across both `compact()`s) sitting unmerged on `feat/memory-compact`
  **after its PR #19 had merged**. Recovered as PR #26. Mechanism is always the same: an agent keeps
  committing to a branch after its PR merges. **Fix: delete merged branches promptly** — 16 are
  merged and fully accounted for. A full content-not-hash sweep of all 8 branches carrying unmerged
  commits found exactly one real loss; the rest were cherry-picked recoveries or renamed symbols
  that only *look* stranded under ancestry checks.
- **`uv run pytest` cannot resolve on this machine.** A locally-installed **Python 3.14** makes
  `edith[voice]`'s `onnxruntime<1.20` pin conflict with `fastembed`'s marker split for
  `python_full_version >= 3.14`. **Pre-existing** — reproduces on bare `origin/master`. Use
  `uv run --frozen`, or `.venv/bin/python -m pytest` directly.
- ~~**`README.md` is stale.**~~ **FIXED (PR #23, merged 2026-07-26):** slices 0–6 all ✅ plus rows for
  the daemon root, Guard, background reasoning and `compact()`; `python -m edith.daemon` leads the
  "Running it" section; Known gaps rewritten around the real four. **It will go stale again the
  moment #24–#28 merge** — its Known-gaps section still says there is no launchd job, no scheduler,
  no menu-bar app, and that Guard gates nothing. Refresh it in the same pass that merges them.
- **Kuzu embedded is single-process (Session 10).** The viewer, finder, and ingest each open
  `memory.kuzu` directly and contend on the on-disk file lock — only ONE may hold it at a time
  (running `--reembed` requires no other EDITH process on the DB). The production fix is routing
  ALL DB access through `edithd` (one owner of the handle; every other surface talks over the
  Control API). Noted, not built — out of scope for the embedding-gap fix.
