# Spike findings — Session tap (spec 04 §Step 0)

**Date:** 2026-07-09 · **Machine:** owner macOS arm64 · **Claude Code version observed:** 2.1.187
**Method:** probed the live machine (this running session + prior transcripts), not a synthetic setup.
Reproduce with `python scratch/spike_session_tap.py`.

## Verdict

**Build the collector as a real-time tail of the Claude Code session transcript JSONL files**
under `~/.claude/projects/<slug>/<sessionId>.jsonl`. This is Candidate B (file tailing), and it
is the clear winner. Every open question in the spec resolves **positively**.

> The killer demo (owner pastes an Airflow error → EDITH narrates what the session does next) is
> **deliverable in v1.** Pasted text lands where the tap can read it, and records are written in
> real time.

## Open questions — resolved

### #1 (the risk): can a tap capture owner-pasted terminal content? → YES

- User prompts — typed **or pasted** — are written **inline** into the `user` record's
  `message.content` as a text block. `promptSource` values seen: `typed`, `system`, `null`
  (skill/system-injected). There is **no separate `paste` source and no `[Pasted text #N +M lines]`
  placeholder externalization** in an 11 MB / 6.6k-record transcript that contains real pastes
  (images, a notebook cell, multi-line debug dumps).
  - Honesty note: the probe's placeholder-regex reports **1** hit, but that hit is the spike
    script's *own* source text (it literally contains the string `Pasted text`) echoed into the
    transcript by the `Write` tool call — not a genuine user paste. Genuine externalization: none observed.
- Image pastes become inline `image` content blocks on the `user` record (2 observed).
- **Residual caveat (do not over-claim):** verified only against CC **2.1.187**. Some Claude Code
  versions externalize *very large* pastes to a placeholder + attachment. The demo's input (an
  Airflow error, a few dozen lines) lands inline here. The collector should therefore **also read
  `attachment` records** (see below) so it degrades gracefully if a future version externalizes.

### #2: latency — per-event or batched at turn end? → PER-EVENT, real time

Records from *this* session, as each tool ran (timestamps UTC; +5:30 = owner-local):

```
07:11:12.620  assistant  tool_use Bash   ← command emitted
07:11:12.864  attachment hook_success    ← PreToolUse hook   (+0.24 s)
07:11:16.145  user       tool_result     ← command returned  (+3.3 s = its runtime)
07:11:31.622  assistant  tool_use Bash   ← next command
07:11:34.570  user       tool_result
```

Each record is flushed within ~1 s of the event, not batched at turn boundary. Meets the
"narrate within ~1–2 s" bar. File `mtime` tracks the live session continuously.

### #3: session-id discovery + enumeration → transcript filenames

- `sessionId` is the transcript filename stem, and is **also** a field on every record.
- Enumerate sessions by listing `~/.claude/projects/*/*.jsonl`. New sessions create new files
  (under a possibly-new slug dir), so the collector must **watch the `projects/` tree dynamically**
  (e.g. `watchdog`), not tail a fixed file list captured at startup.

### #4: repo detection → best-effort from `cwd` / `gitBranch`

Every record carries `cwd` and `gitBranch`. Map `cwd` → repo name (basename, or match against
known `~/gitstuff/*` clones). **Best-effort only** — the sampled records showed `cwd:
/Users/akhilsingh`, `gitBranch: HEAD`, which maps to *no* repo. The `repo` field in both bus
payloads is nullable; keep it that way.

### Candidate A (Claude Code hooks) — works, but NOT the primary

`~/.claude/settings.json` configures firing hooks for `SessionStart`, `SessionEnd`,
`UserPromptSubmit`, `SubagentStart/Stop` (and `PreToolUse`/`PostToolUse`/`Stop`/`PreCompact`
across plugins). `UserPromptSubmit` **does** fire on prompt submit. But to use hooks as a tap,
EDITH would have to register *its own* hook command machine-wide and run a listener. Transcript
tailing needs **zero config**, is **cross-session** (sees every session, not just ones that
opted in), is **structured**, captures the same events **plus tool results**, and is real-time.
→ **Hooks are a possible future low-latency enhancement for prompt events; not needed for v1.**

## Event taxonomy the collector gets for free (maps to spec payloads)

| Transcript record | → `session.event.kind` / `session.state.state` |
|---|---|
| `user` w/ `promptSource=typed`/paste | `kind=prompt` (this is the paste-capture path) |
| `assistant` content block `tool_use` (has `name`) | `kind=tool_use` (silent class by default) |
| `user` content block `tool_result` (`is_error`) | error signal → `state=error` |
| `system` w/ `stopReason` / `level=error` / `hookErrors` | `kind=stop` / `state=error` |
| first record for a `sessionId` (or `SessionStart` attachment) | `kind=start`, `state=working` |
| no records for a session for N s | `state=idle` / `waiting` (timer, not a record) |
| `attachment.type` ∈ {`command_permissions`, `plan_mode_exit`} | permission/plan-gate state (bonus) |

Per-record metadata always present: `sessionId`, `cwd`, `gitBranch`, `timestamp`, `promptSource`,
`permissionMode`, `version`.

## Secrets (spec §Autonomy & secrets — the highest-risk surface)

Transcripts contain everything: pasted DB URIs with passwords, `.env` values echoed by tools, API
keys in tool output. **`Guard.redact` / `sanitize_text` MUST run on every raw line at read time,
before it touches the bus, a model call, a log, or Memory.** SessionBus never persists raw lines;
its in-memory `session_states` map holds only redacted summaries. (`edith/memory/secrets.py`
`sanitize_text` already covers AWS/Google/Slack/`sk_`/connection-string shapes — reuse it as the
choke-point; extend if the spike surfaces a shape it misses.)

## Decision for Step 1 (collector implementation)

1. **Primary tap:** `watchdog` observer on `~/.claude/projects/`; per file, a JSONL tailer that
   reads appended lines, parses each record, and emits normalized raw events. Read `user`,
   `assistant`, `system`, and `attachment` records; ignore UI-only types (`mode`, `ai-title`,
   `last-prompt`, `pr-link`, `permission-mode`, `file-history-snapshot`).
2. **Redact at read** via `sanitize_text` before anything leaves the collector.
3. **Injectable seam:** the collector is behind the `session.event`/`session.state` interface, so
   SessionBus + narration are testable headlessly by feeding synthetic records (no real `~/.claude`
   needed in tests). The live tail is the owner-smoke surface, same pattern as voice `live.py`.
4. Enumeration is dynamic (watch the tree); repo is best-effort/nullable; idle/waiting is a timer.

## Scope confirmation

Bare-shell (non-OMC/Claude) sessions produce no transcript and no hooks → **out of scope for v1**
(matches spec open question). Only sessions that appear under `~/.claude/projects/` are observable.
