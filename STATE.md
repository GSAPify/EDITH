# EDITH — Build State

> Machine-and-human readable status. Update this at the end of every session (or at ~90% context).
> This is the first file a new session reads after `SESSION-PROTOCOL.md`.

**Current phase:** Slices 0–6 DONE + Viewer + Ingest + NL-finder + Workspace-graph + voice conversation mode + **daemon composition root ("she talks back", Session 18)** + **Guard / router-background seam / `Memory.compact()` (Session 19, PRs #17–#19 merged)**. **ALL NUMBERED SLICES BUILT + all three long-standing deferred seams closed.** What remains is NOT slice work — it is **operationalization** (see §Next action): EDITH is a hand-started foreground process, not an always-on daemon, and nothing keeps the graph fresh on a schedule.
**Session 19 (2026-07-25) — three deferred seams merged.** `master` = `3131d33`. **PR #17 Guard** (`edith/guard/guard.py`: pure `authorize`/`Decision` + windowed token budget, north-star §6 — policy object only, **not yet injected into any subsystem**; every `authorize()`/`budget_check` seam still defaults to ALLOW). **PR #18 router background** (`edith/router/background.py`: `supervised_reason` draft→review, fully consumable; `think_async` free function = **seam only, no production consumer**). **PR #19 `Memory.compact()`** (bounded conv-Fact eviction in `store.py` + `vector.py`). **332 passed, 2 skipped on master; `ruff check .` has 3 pre-existing SIM115 in `scripts/sagemaker/train_hey_edith.py` (non-`edith/`, predates these merges) — `edith/` + `tests/` are clean.** **⚠ OPEN PR #20** (`feat/router-background-opus`, spec-11-as-written `BackgroundReasoner` + Brain triggers + daemon wiring — background opus WITH a real consumer) is **currently unmergeable**: it collides with PR #18 on `edith/router/background.py`, `edith/router/__init__.py`, `tests/test_router_background.py` (all "added/changed in both"), and its `docs/specs/11-background-reasoning.md` collides with the merged `docs/specs/11-guard.md`. Resolution is a design call, not a mechanical merge — see §Next action.
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
| — | Guard (autonomy + budget) | (11) | ✅ built, ⚠ not wired | PR #17. `edith/guard/guard.py`: pure policy object — `authorize(action, needs_confirmation) → Decision{ALLOW,ASK,DENY}` (denylist) + windowed token budget (`token_budget`, `window_seconds`, injected `clock`). No I/O, no model calls, no bus → trivially testable, construct once + inject. Does NOT duplicate redaction (§6.1 stays with `sanitize_text` at the Router choke-point). **134 test lines / suite green.** ⚠ **Nobody constructs it yet:** `Narrator.budget_gate`, `Router.budget_check`, and every desktop executor's `authorize()` still default to ALLOW. Wiring it in is the follow-up — see §Next action. |
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

### ▶ NEXT — operationalization, not slices (START HERE)

All numbered slices are built and all three long-standing seams merged (#17/#18/#19). The gap between
"the code works" and "EDITH is the always-on presence in `00-north-star.md`" is now **four operational
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
Design notes before building it: incremental skip already keys off `Repo.last_commit_date`; the
metadata pass is model-free so a weekly run is ~free, but **deep extract is not** (Opus per repo) —
gate any scheduled deep pass on Guard's budget, and don't schedule it across 1297 repos. It **must**
not run while `edithd` holds the Kuzu handle (single-process) → either stop/start the daemon around
it, or route ingest through the Control API. That contention is the real design decision here.

**3. Wire Guard in (it's built but injected nowhere).** `Narrator.budget_gate`,
`Router.budget_check`, and every `edith/desktop` executor `authorize()` still default to ALLOW.
One `Guard` constructed in the composition root and passed to those seams closes north-star §6.
This is also the throttle any background-opus work (PR #20) and any scheduled deep-extract needs.

**4. Menu-bar control — not built.** north-star §1/§4.2 make it the only visible surface
(pause/resume/kill + a status label). The Control API side exists (`edith/daemon/control.py`,
unix socket, 0600) and `edith/daemon/client.py` can drive it; there is **no** menu-bar app (no
`rumps`/`pystray`/`NSStatusBar` anywhere). Today pause/resume/kill = the CLI client only.

**Also pending, smaller:** resolve **PR #20** (design call: does `BackgroundReasoner.think_async`
supersede master's free-function `think_async`, or coexist? Recommended: supersede — PR #20's is
tracked + budget-gated + has a real consumer, master's is explicitly "the seam, not the feature" —
and KEEP master's `supervised_reason`, which PR #20 doesn't provide. Renumber its spec 11 → 13,
`11-guard.md`/`12-router-background.md` are taken). Delete the ~10 merged remote branches.
Owner LIVE-SMOKE still outstanding for: desktop-control OS actions (Spotify/Terminal/OMC) and the
full `edith.daemon` voice loop.

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
`think_async` with a production consumer (PR #20, conflicted); `resolve_tier`'s `suggest_background`
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

## Known limitations

- **EDITH is not actually always-on (Session 19).** She exists only while a terminal holds
  `python -m edith.daemon`. No launchd job, no `deploy/*.plist` (the spec-01 claim that one shipped
  is wrong — no `.plist` exists in the repo or its history), no login-item, no menu-bar surface.
  Every "ambient / always-on presence" claim in `README.md` and `00-north-star.md` is design intent,
  not current behavior.
- **The graph does not refresh itself (Session 19).** No cron / launchd / scheduler anywhere; no
  weekly (or any) automated `--workspace` pass. `~/.edith/data/memory.kuzu` was last written
  **2026-07-12** and drifts from the 1378 real repos every day it isn't re-run by hand. The
  blocker to automating it is the Kuzu single-process limitation below (a scheduled ingest and a
  running `edithd` cannot both hold the handle).
- **Guard exists but gates nothing (Session 19).** `edith/guard/guard.py` is built and tested; no
  subsystem constructs it, so `authorize()`/budget seams still default to ALLOW everywhere.
- **`README.md` is stale.** Its status table still shows Slice 5 as "⬜ next" and Slice 6 unstarted,
  quotes 196 tests, and lists "No `edithd` composition root" under Known gaps — all superseded.
  Refresh it alongside the next merge (its own small PR).
- **Kuzu embedded is single-process (Session 10).** The viewer, finder, and ingest each open
  `memory.kuzu` directly and contend on the on-disk file lock — only ONE may hold it at a time
  (running `--reembed` requires no other EDITH process on the DB). The production fix is routing
  ALL DB access through `edithd` (one owner of the handle; every other surface talks over the
  Control API). Noted, not built — out of scope for the embedding-gap fix.
