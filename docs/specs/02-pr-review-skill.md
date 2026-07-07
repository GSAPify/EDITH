# 02 — PR-Review Skill

> **Honest-framing reminder:** no unicorns. "Unlimited context" = memory + retrieval +
> compaction; "two agents in one inference / haiku talks while opus thinks" = orchestration
> of two model calls (fast masks slow). Nothing in this file implies a capability that does
> not exist today.
>
> This slice follows `_TEMPLATE.md`. Architecture-level interfaces and cross-cutting rules
> are fixed in `00-north-star.md` — they are referenced, not restated, here. This file adds
> implementation depth for Slice 2 only.

## Terminology (glossary)

| Term | Meaning |
|------|---------|
| **EDITH** | The system. Always-on local-first macOS assistant. |
| **edithd** | The daemon process that runs everything under the hood. |
| **bus** | In-process event/message bus; components `publish`/`subscribe`. |
| **Guard** | Cross-cutting enforcement: `redact`, `authorize` (allow/ask/deny), budget. |
| **Router** | `model_call(messages, tier_hint) -> response` over the Bifrost adapter. |
| **Memory** | Graph + vector store: `recall` / `remember` / `compact`. |
| **Skill** | Capability with `name`, `triggers`, `needs_confirmation`, `run(context)->result`. |
| **tier** | Model size class the Router selects: haiku / sonnet / opus. |
| **owner** | Akhil Singh — the single user of this local-first system. |

---

## Purpose

This slice delivers the first real autonomous action EDITH can complete end-to-end: given a
fuzzy voice or text command like "EDITH, review Tavishi's PR", it resolves the person, finds
the PR, fetches the diff, routes a deep review to opus, and surfaces findings — pausing to
ask before it touches any shared state (posting a review, tagging someone, commenting on
GitHub). Shipping this proves the full action loop: Memory recall → tool use → model call →
Guard gate → optional write.

## Scope

**In:**
- Person resolution from name or alias ("Tavishi" → GitHub handle + relevant repos)
- PR location via `gh` CLI (searching by assignee, author, or linked Slack discussion)
- Diff fetch and review via Router (opus for deep analysis)
- Findings surfaced to the owner via `speak()` and/or a structured text summary
- Confirm gate before any write to GitHub (post review, add comment, request changes)
- Memory: record person → repo → review-history so next invocation is faster
- Learning the owner's review style over time (patterns he approves vs. flags)

**Out:**
- Merging PRs (a separate, higher-risk action — not this slice)
- Automated CI/lint runs (delegated to GitHub Actions; EDITH reads results, doesn't run them)
- Multi-repo batch reviews (scope = one PR per invocation in v1)
- Any Slack-send in the owner's name without confirmation (ASK gate applies)

---

## Canonical Flow

```
Owner: "EDITH, review Tavishi's PR"
         │
         ▼
┌────────────────────────────────────────────────────────────────────────┐
│  BRAIN receives voice.utterance event                                  │
│  matches trigger: ["review", "PR"] → dispatch PRReviewSkill           │
└──────────────────────────────┬─────────────────────────────────────────┘
                               │ run(context)
                               ▼
┌────────────────────────────────────────────────────────────────────────┐
│  STEP 1 — Resolve person                                               │
│                                                                        │
│  Memory.recall("Tavishi") ──► hit? ──► GitHub handle + known repos    │
│                         │                                              │
│                         └── miss? ──► ASK owner: "Who is Tavishi on   │
│                                       GitHub? Which repo?"            │
└──────────────────────────────┬─────────────────────────────────────────┘
                               │ handle + repo(s) known
                               ▼
┌────────────────────────────────────────────────────────────────────────┐
│  STEP 2 — Locate the PR                                                │
│                                                                        │
│  gh pr list --author <handle> --repo <repo> --state open              │
│       │                                                                │
│       ├── 1 result ──► proceed                                         │
│       ├── 0 results ──► try Slack: search recent messages in known    │
│       │                 channels for "PR" + "Tavishi" → extract URL   │
│       │                 still nothing? ──► ASK owner                  │
│       └── >1 result ──► ASK owner: "Found N open PRs — which one?"   │
└──────────────────────────────┬─────────────────────────────────────────┘
                               │ PR URL / number confirmed
                               ▼
┌────────────────────────────────────────────────────────────────────────┐
│  STEP 3 — Fetch diff + context                                         │
│                                                                        │
│  gh pr diff <number> --repo <repo>  (stdout → diff text)              │
│  gh pr view <number> --repo <repo>  (description, CI status, comments)│
│  Memory.recall(repo) ──► known patterns / past review notes           │
└──────────────────────────────┬─────────────────────────────────────────┘
                               │ diff + context assembled
                               ▼
┌────────────────────────────────────────────────────────────────────────┐
│  STEP 4 — Review (Router → opus)                                       │
│                                                                        │
│  Guard.redact(diff)   ← strip secrets before model call               │
│  Router.model_call(                                                    │
│    messages=[system_prompt, diff, past_style_notes],                  │
│    tier_hint="opus"                                                    │
│  ) → findings                                                          │
│                                                                        │
│  While opus is thinking:                                               │
│    haiku call → speak("Fetching Tavishi's PR, reviewing now...")       │
│    (two-call latency masking — north-star §3)                          │
└──────────────────────────────┬─────────────────────────────────────────┘
                               │ findings ready
                               ▼
┌────────────────────────────────────────────────────────────────────────┐
│  STEP 5 — Surface findings to owner                                    │
│                                                                        │
│  speak(summary)  — top 2-3 issues aloud, full list in text            │
│  publish("skill.result", { skill: "pr-review", findings })            │
└──────────────────────────────┬─────────────────────────────────────────┘
                               │
                               ▼
┌────────────────────────────────────────────────────────────────────────┐
│  STEP 6 — CONFIRM GATE (shared-state write)                            │
│                                                                        │
│  Guard.authorize("post_github_review") → ASK                          │
│                                                                        │
│  speak("Should I post this review on GitHub?")                        │
│                                                                        │
│  owner confirms ──► gh pr review <number> --body <findings>           │
│  owner declines ──► findings stay local; Memory.remember() anyway     │
└────────────────────────────────────────────────────────────────────────┘
                               │
                               ▼
┌────────────────────────────────────────────────────────────────────────┐
│  STEP 7 — Memory update                                                │
│                                                                        │
│  Memory.remember({                                                     │
│    person: "Tavishi", handle: "<gh-handle>",                          │
│    repo: "<repo>", pr: "<number>",                                     │
│    reviewed_at: <ts>, owner_edits: <any changes owner made>           │
│  })                                                                    │
│  → next "review Tavishi's PR" skips Steps 1-2 resolution entirely     │
└────────────────────────────────────────────────────────────────────────┘
```

---

## Ask-When-Unsure Behavior

The skill never guesses when a resolution is ambiguous. Specific triggers that force an ASK:

| Situation | What EDITH asks |
|-----------|-----------------|
| Person name not in Memory + no clear GitHub match | "I don't have Tavishi in my contacts — what's their GitHub handle?" |
| Person known but active in multiple repos | "Tavishi has open PRs in `repo-a` and `repo-b` — which one?" |
| Multiple open PRs from that person in one repo | "Found 3 open PRs from Tavishi — \#12 (auth), \#15 (fix), \#17 (docs). Which?" |
| PR found but CI is still running | "CI is still running on \#15. Review the diff now or wait?" |
| Findings unclear due to huge diff (>2000 lines) | "This diff is large. Review the full thing with opus (costs more) or just the changed modules?" |

Guessing + being wrong is more disruptive than asking once. This behavior is intentional.

---

## Skill Contract

Implements the Skill interface from north-star §4.3:

```python
class PRReviewSkill:
    name: str = "pr-review"

    triggers: list[str] = [
        "review",       # "review Tavishi's PR"
        "check pr",     # "check the PR"
        "look at pr",   # "look at Tavishi's pull request"
        "pull request", # direct mention
    ]

    needs_confirmation: bool = True  # always ASK before posting to GitHub

    async def run(self, context: SkillContext) -> SkillResult:
        # context carries: raw utterance, resolved intent, Memory handle
        # returns: { findings, pr_url, posted: bool, remembered: bool }
        ...
```

**Brain dispatch path:**

```
voice.utterance event
  → Brain checks triggers against each registered Skill
  → PRReviewSkill.triggers match
  → Brain calls PRReviewSkill.run(context)
  → skill publishes skill.result on completion
  → Brain consumes skill.result, routes to speak() / optional write
```

The Brain does not own review logic. It dispatches to the skill and reacts to `skill.result`.
The skill owns: resolution, tool calls, model call, confirm gate, memory update.

---

## Interface to edithd

- **Inputs:**
  - `voice.utterance` event (topic from bus) containing raw owner command
  - `brain.decision` event dispatching to this skill (Brain → Skill)
- **Outputs:**
  - `skill.result` (topic: `skill.result`) — findings + metadata published to bus
  - Optional: `gh pr review` subprocess call (after owner confirms)
  - `speak(text)` calls at Steps 1, 4 (ack), and 6 (confirm prompt)
- **Bus events:**

  | Topic | Direction | Payload shape |
  |-------|-----------|---------------|
  | `voice.utterance` | subscribe | `{ ts, source:"voice", payload:{ text } }` |
  | `brain.decision` | subscribe | `{ ts, source:"brain", payload:{ skill:"pr-review", intent } }` |
  | `skill.result` | publish | `{ ts, source:"pr-review", payload:{ findings, pr_url, posted, remembered } }` |

- **Control contracts:** none — this skill does not touch the Control API.

---

## Data Model

Memory nodes and edges this skill reads and writes:

```
(Person { name, gh_handle, slack_handle? })
    │
    │ WORKS_ON
    ▼
(Repo { name, org, default_branch })
    │
    │ HAS_PR
    ▼
(PR { number, url, title, state, reviewed_at })
    │
    │ REVIEWED_BY
    ▼
(Review { ts, findings_summary, posted_to_gh, owner_edits })
```

**Vector records:** the `findings_summary` text of each past review is embedded and stored so
`Memory.recall("Tavishi auth PR patterns")` can surface relevant style notes at Step 4.

---

## Dependencies

- **Other slices:**
  - **Slice 1 (Memory + Brain)** — mandatory. Memory.recall/remember and the Brain dispatch
    loop must exist before this skill can run.
  - **Slice 5 (Router)** — consumed via `model_call` contract. Until Slice 5 ships, a
    single-tier passthrough (direct opus call) is acceptable per north-star §7.
- **Libraries:**
  - `gh` CLI — already installed on the owner's machine. Used for all GitHub I/O (pr list,
    pr diff, pr view, pr review). No PyGitHub or REST calls needed; `gh` is the right tool.
  - `subprocess` / `asyncio.create_subprocess_exec` — to shell out to `gh` safely.
  - OMC's existing `code-review` skill — **reuse, don't reinvent.** The review prompt and
    findings-formatting logic from OMC's `/code-review` are proven. Wrap or call them rather
    than authoring a new review rubric from scratch. This is the single most important
    build-time shortcut available.
  - Slack MCP (already wired in the owner's OMC environment) — for PR discovery when `gh`
    returns zero results.

---

## Tech Choices

Slice-specific only; stack-wide choices are in north-star §5.

| Choice | Justification |
|--------|---------------|
| `gh` CLI for all GitHub I/O | Already installed; handles auth via macOS Keychain natively; simpler than REST. |
| `asyncio.create_subprocess_exec` for `gh` calls | Non-blocking; keeps `edithd`'s async loop healthy; avoids blocking the bus. |
| Reuse OMC `code-review` skill for review logic | Owner already has it working, it matches his style, and it avoids maintaining two review rubrics. |
| opus for deep review | Review quality is the point; sonnet is acceptable for tiny diffs (<50 lines), but opus is the default. Gate explicitly on diff size. |
| haiku for immediate ack | Masks the opus latency per north-star §3 two-call pattern. |

---

## Autonomy & Secrets Notes

- **Autonomy gate** (per north-star §6.3):

  | Action | Gate |
  |--------|------|
  | `gh pr list` (read) | AUTO |
  | `gh pr diff` (read) | AUTO |
  | `gh pr view` (read) | AUTO |
  | Memory.recall (read) | AUTO |
  | Router.model_call (opus review) | AUTO — but gated on diff size / cost check |
  | `gh pr review --body <findings>` (write to GitHub) | **ASK** — shared-state write |
  | Slack message in owner's name | **ASK** (not in this slice's scope anyway) |
  | Memory.remember (local write) | AUTO |

- **Secrets:** the `gh` CLI manages its own GitHub token via macOS Keychain — EDITH never
  touches or stores it. The diff text may contain API keys, env values, or tokens checked into
  code; `Guard.redact(diff)` runs on the full diff before it is included in any model call
  payload. The review findings returned by opus are stored in Memory as a summary only —
  never the raw diff.

---

## Cost / Token Notes

Deep review with opus is the most expensive call in the system. Gate it deliberately:

- **Haiku** handles the immediate acknowledgement ("Fetching Tavishi's PR, reviewing now...")
  and any simple resolution questions (no model needed if Memory has a clean hit).
- **Opus** fires once per review request, on the assembled diff + context. This is intentional
  and expected — review quality is the whole point of the skill.
- **Size gate:** if the diff exceeds ~2000 lines, ASK the owner before spending a large opus
  call. Offer the option of reviewing only changed modules.
- **No repeated opus calls** within one invocation — assemble all context first, then single
  call. Do not stream multiple partial reviews.
- **Budget impact:** one PR review ≈ one expensive opus call. At typical Pattern/Bifrost
  limits this is fine, but the Guard budget tracker should count it. Surface in `status`.

---

## Build Steps (High-Level, Ordered)

1. Confirm Slice 1 (Memory + Brain) is running and the Skill dispatch interface is wired.
2. Scaffold `PRReviewSkill` class: `name`, `triggers`, `needs_confirmation`, stub `run()`.
3. Register the skill in Brain's skill registry so trigger matching works.
4. Implement person resolution: `Memory.recall(name)` → hit path + ASK path.
5. Implement PR location: `gh pr list --author` → single/zero/multiple branch + Slack fallback.
6. Implement diff + context fetch: `gh pr diff` + `gh pr view` via `asyncio.create_subprocess_exec`.
7. Wire haiku ack call (speak "reviewing now") before opus fires.
8. Implement review call: `Guard.redact(diff)` → `Router.model_call(tier_hint="opus")`.
9. Implement confirm gate: `Guard.authorize("post_github_review")` → ASK → `gh pr review` on confirm.
10. Implement `Memory.remember()` write (Step 7 in the flow) — runs whether or not the owner posts.
11. Wire `skill.result` publish and `speak(summary)` output.
12. Test the full happy path (person in Memory, one PR open, owner confirms post).
13. Test the ask paths: unknown person, multiple PRs, owner declines post.

---

## Verification / Testing

Manual checks to run at build time (no app UI — verify via daemon logs + `speak()` output):

```bash
# 1. Trigger via stdin / test harness (voice not yet wired at Slice 2)
#    Emit a voice.utterance event directly on the bus:
python -c "from edith.bus import publish; publish('voice.utterance', {'text': 'review Tavishi PR'})"

# 2. Confirm Memory recall fires (should hit if Tavishi was seeded, ask if not)
# Expected speak(): "Who is Tavishi on GitHub?" (miss) or "Fetching Tavishi's PR..." (hit)

# 3. Confirm gh CLI call is made (check daemon debug log)
# Expected log line: [pr-review] gh pr list --author <handle> ...

# 4. Confirm Guard.redact runs before model call
# Seed a fake diff with a credential string; verify it does not appear in the opus payload

# 5. Confirm confirm gate fires before any gh pr review write
# Expected speak(): "Should I post this review on GitHub?"

# 6. Confirm Memory.remember writes after the flow (with and without posting)
# Expected graph node: (PR { number, reviewed_at, ... }) linked to (Review { ... })

# 7. Confirm skill.result event published on bus
# Subscribe in test harness; assert findings key is non-empty
```

---

## Open Questions

- **OMC code-review reuse path** — The OMC `/code-review` skill runs inside a Claude Code
  session. For EDITH to reuse it, the cleanest path is to extract the review prompt/rubric
  as a standalone Python function or prompt template. Is the owner OK with that extraction,
  or should EDITH call it via a spawned OMC agent instead? The latter is heavier but keeps the
  rubric in one place.

- **Slack PR discovery scope** — When `gh` returns zero results, the fallback searches Slack
  for recent PR mentions. Which channels should be searched by default? The owner's working
  channels (e.g. eng, reviews, team DMs) need to be seeded in Memory or config. Who populates
  the initial channel list?

- **Review-style learning** — Memory stores owner edits to reviews over time. What is the
  minimum useful signal? (e.g. "owner deleted findings X and Y → those are noise", "owner
  added finding Z → look for that pattern".) Needs at least 3-5 real reviews before it
  meaningfully influences the prompt. Spec the feedback loop more concretely at build time.

- **Diff size threshold** — 2000 lines is a placeholder for the "ask before large opus call"
  gate. Owner should confirm the right number based on typical PR sizes in his repos.

- **Slack MCP availability** — The Slack MCP is available in OMC sessions. Confirm it is
  accessible to `edithd` at runtime (not just in interactive Claude Code sessions) before
  wiring the Slack fallback in Step 2.

---

## Completion Record — PR-Review Skill — 2026-07-07 (Session 12)

- **What shipped:** `PRReviewSkill` — EDITH's first end-to-end autonomous action. The full
  7-step flow: resolve person from Memory → locate the open PR via `gh` → fetch the diff →
  redact → route a deep review to Opus → surface findings → **confirm gate** → remember. Plus
  the Skill contract + a trigger-match dispatch registry wired into Brain (the interface the
  spec said had to exist first — it didn't; now it does).
- **How it works:** Brain matches an utterance against each registered Skill's `triggers`
  (case-insensitive substring); the first match owns the turn, runs, publishes `skill.result`,
  and short-circuits the recall→answer path. An empty registry (the default) leaves Brain
  identical to before. The skill injects all deps (Router, `gh` runner, `confirm`, `speak`) so
  it runs fully offline in tests. The **confirm gate is the crux**: the `gh pr review` write is
  the *only* GitHub-write call site and lives inside a single `if await self._confirm(...)`
  branch — unreachable unless confirm returns True. Default confirm is `_deny` (never posts).
  The diff is `sanitize_text`-redacted *before* the model message is assembled, so a secret in
  checked-in code never reaches Opus. Memory is written whether or not the review is posted, and
  the Person node is stored with its `gh_handle` so the next invocation is an instant HIT.
- **Key decisions made during build:**
  - **`Person.gh_handle` added additively** (guarded `TABLE_INFO` → `ALTER … ADD` migration).
    The ingested Person nodes had `{name}` only; the handle is required to run `gh pr list
    --author` and to make Step-7 "faster next time" real. Migration verified non-destructive on
    the live DB (26 Person / 23 Repo / 145 Fact intact).
  - **Confirm gate defaults to DENY**, not to a blocking prompt. Voice/interactive confirm is
    Slice 3/4; until then the honest Slice-2 behavior is "review, surface, remember, don't post."
  - **Inline Opus review rubric** rather than shelling out to OMC `/code-review` from `edithd`
    (that integration is heavier; kept the slice shippable).
- **Deviations from spec + why:** (1) No haiku *model* call for the ack — `speak()` fires the
  "reviewing now…" line directly (a real haiku call adds latency/cost for no user-visible gain
  at Slice 2; the two-call latency-mask is fully realized in Slice 5). (2) Slack PR-discovery
  fallback (Step 2, zero-results branch) deferred — v1 ASKs the owner instead; Slack-MCP-at-
  runtime is still an open question. Both noted as follow-ups.
- **Files created / changed:** NEW `edith/skills/{__init__,base,gh,pr_review}.py`; EDIT
  `edith/brain/loop.py` (dispatch), `edith/memory/store.py` (gh_handle + migration). Tests:
  NEW `tests/test_{pr_review_skill,brain_skill_dispatch,skills_gh,person_gh_handle_migration}.py`.
- **Verification / tests run + results:** 130 passed + 1 skipped (was 114+1; +16 new),
  `ruff check` clean, `pyright` 0 errors. Mandatory tests confirmed non-vacuous by reading
  source: `test_declined_never_posts` (confirm→False ⇒ `review_calls == []`) and
  `test_planted_secret_redacted_before_router` (asserts secrets present in raw diff, absent from
  the Router payload). **LIVE smoke:** real `gh` + real Bifrost Opus against `patterninc/agents`
  PR #2423 (kemenyc, +28/-2), `confirm=deny` — Opus produced a genuine review (caught a real
  always-on→toggle-gated regression), `posted=False`, and the recorded `gh` calls were exactly
  `pr list` + `pr diff` with **zero** `pr review` write.
- **Follow-ups / known gaps:** OMC `/code-review` rubric reuse; Slack PR-discovery fallback +
  confirm Slack-MCP reachable from `edithd` at runtime; diff-size gate (>2000 lines ⇒ ASK before
  a large Opus call) — the size-branch isn't wired yet; review-style learning loop (needs 3-5
  real reviews). Kuzu single-process lock still applies (stop the viewer before opening the DB).
