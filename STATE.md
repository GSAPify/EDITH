# EDITH — Build State

> Machine-and-human readable status. Update this at the end of every session (or at ~90% context).
> This is the first file a new session reads after `SESSION-PROTOCOL.md`.

**Current phase:** Slices 1, 2 (PR-review), **3 (Voice)** DONE + Viewer + Ingest + NL Finder. Next: Slice 4 (Session awareness) or 5 (Router).
**Active slice:** → next is **Slice 5 (Router)** or **Slice 4 (Session awareness)**. Slice 3 (Voice) built by an OMC tmux team (3 executor workers), lead-verified, on branch `build/slice-3-voice` (committed this session; PR-ready). Voice core is headless-tested; audio path is owner LIVE-SMOKE surface (mic/wake/STT/playback seams + ElevenLabs key + `brew install portaudio` + `.[voice]` extra) — see spec 03 §Follow-ups.
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
| 4 | Session awareness | ✅ done | ⬜ not started | Highest uncertainty — spec mandates a spike first |
| 5 | Router | ✅ done | ⬜ not started | Two-call latency masking (orchestration, not one inference) |
| 6 | Desktop control | ✅ done | ⬜ not started | Own-shell for OMC launches; Terminal.app osascript for visible term |
| — | Memory viewer | (07) | ✅ done | Offline local graph viewer: `MemoryStore.graph_snapshot()` + `edith/viewer/` (stdlib 127.0.0.1 server, vendored force-graph UMD, `--demo` seeder, `python -m edith.viewer`). **70 tests + 1 live-skipped, ruff/pyright clean.** Zero new runtime deps. Reads live Memory; repo ingestion populates it for real. |
| — | Repo ingestion | (08) | ✅ done | `edith/ingest/` populates the LIVE graph from local `patterninc` clones: discover→fetch→**REDACT (choke-point)**→Sonnet classify/Opus deep→map→`remember`. `python -m edith.ingest [--dry-run] [--repos] [--limit] [--data-dir] [--max-tokens]`, incremental skip on `Repo.last_commit_date`, secret-safe status report, one-time global `~/.claude/CLAUDE.md` owner context. Additive schema (`Repo` +4 cols, `Fact.source`, `authored_by` Repo→Person). **97 tests + 1 live-skipped, ruff/pyright clean.** Live smoke: 58 nodes to a temp dir, secret-scan clean. Full contributed-repos run is orchestrator-gated pending review. |
| — | NL finder + resolve-on-miss | (09) | ✅ done | `edith/finder/`: `find_repos` (model-free semantic+graph fuse → `relates_to` walk → rank by strength+degree; **Session 10:** per-token graph fallback fires when both signals score zero so a populated graph never silently returns nothing) + `summarize_hits` (Sonnet, injected); `python -m edith.finder "query"`. **Session 10:** ingest now writes via `VectorMemoryStore` so live Facts ARE embedded; `python -m edith.ingest --reembed` backfills existing graph-only Facts (local embedder, no model cost, idempotent). Live: 145 Facts reembedded, `finder "seo tools"` returns real repos. `resolve_repo` = HIT (graph `repo-<name>`) / RESOLVED (local clone or `gh` README → **REDACT choke-point** → fast Sonnet answer NOW + **background Opus** deep-extract coroutine the caller runs via `asyncio.create_task`, Slice-5 `think_async` seam) / NOT_FOUND (clean, no model). Thin Brain hook: recall-miss + repo mention + injected resolver → resolve then answer (**default `None` = no-op**, existing tests unchanged). Reuses ingest fetch/extract/graph_map. **110 tests + 1 live-skipped, ruff/pyright clean; planted-secret test proven non-vacuous.** Live smoke: `agentsmith` ingest (real Bifrost, relevance 0.72 Opus) → finder ranked it #1 with a real Sonnet summary; resolve HIT path no-model. |

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

### ▶ SLICE 3 — Voice (START HERE)
Slice 2 is done, verified, and live-smoked (see the Completion Record in `docs/specs/02-pr-review-skill.md`).
Next is **Slice 3 (Voice)** — read `docs/specs/03-voice.md`. ElevenLabs primary + local neural fallback
for TTS; local wake-word + STT. The bus seams already exist: Brain publishes `brain.decision` and
skills call an injected `speak()` (currently `_silent` no-op in `edith/skills/pr_review.py`) — Slice 3
wires a real VoiceIO to those. The confirm gate is where voice pays off: the Slice-2 `confirm` callable
(default `_deny`) becomes a spoken "Should I post this review?" → owner voice/keyword → True/False.

**Reuse, don't rebuild:** `edith/router` (Tier.HAIKU for cheap TTS-prep/acks), `edith/skills`
(inject a real `speak`/`confirm` in place of the `_silent`/`_deny` defaults), `edith/brain` (already
subscribes to voice bus topics; `voice.utterance` is its input event), `edith/daemon` (VoiceIO/SessionBus
wiring seam noted in edithd).

**Gotchas the orchestrator MUST heed (still true):**
- Delegated agents return a terse "Complete." — **verify independently + do a LIVE run.** Tests
  green ≠ works (the ingest→finder embed bug passed 110 tests but returned nothing live; Slice 2's
  agent even ended with a confused "send me the port" message — the code was fine, but only reading
  the source + a live smoke proved it).
- **Kuzu is single-process** → stop the viewer (`lsof -ti tcp:8765 | xargs kill`) before running
  anything that opens `memory.kuzu` (hit this again in Slice 2's migration check).
- Bifrost creds are in gitignored `.env` (source it: `set -a; source .env; set +a`). **KEY STILL
  NEEDS ROTATING** (pasted in chat 2026-07-06).
- The owner's Pattern commit identity ≠ GSAPify (author filters unreliable) → resolve people via
  the graph + `gh pr list`, not `--author=GSAPify`.

**Slice-2 seam for Slice 3 to grab:** `PRReviewSkill(router, *, gh, confirm, speak, org)` — `confirm`
and `speak` are injected; the daemon wires the real voice ones. `Person.gh_handle` now exists (guarded
migration in `MemoryStore._migrate_person_gh_handle`). Skill dispatch is `Brain(skills=[...])`; empty
registry = pre-skill behavior.

**Deferred Slice-1 seams** (pick up when their slice needs them, not blocking Slice 2):
`compact()` (needs Session/Conversation node tables + token-counted working buffer); **Guard**
(`authorize`/budget — Brain redacts inline as the interim; `budget_used=0` stub in Control API);
encrypted-volume mount (LocalSecureStore enforces 0700 dev dir); VoiceIO/SessionBus wiring
(Brain already subscribes to their bus topics). Router two-call masking / `think_async` /
tier heuristics = Slice 5.

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
