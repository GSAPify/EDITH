# 14 — Log redaction (research)

> **Status: RESEARCH, nothing built.** This is the investigation behind the security review's
> MEDIUM-1 ("logs are not redacted"). It ends in a recommendation, not an implementation.
>
> **Headline: the finding that matters is not "add redaction to logging".** It is that
> `sanitize_text` matches *credential shapes*, not *sensitivity* — so redaction solves one of
> the two problems hiding under this heading and is the wrong tool for the other.

## Why this is open

`edith/memory/secrets.py::sanitize_text` is the redaction choke-point for model, TTS and bus
payloads. **It has never run on a log record.** Before the LaunchAgent (spec 01 addendum) that
meant a line scrolled past in a terminal. After it, the plist sends the daemon's stdout and
stderr to `~/.edith/logs/edithd.{out,err}.log` — a permanent, unrotated file that until this
session was group-readable.

## What is actually true today (measured, not assumed)

**No handler is configured anywhere in `edith/`.** `grep -rn "basicConfig\|addHandler\|
StreamHandler\|dictConfig"` returns nothing. Logging therefore falls to Python's `lastResort`
handler, which was verified in-process to be:

- **level `WARNING` (30)** — every `_log.info(...)` in the codebase is silently discarded
- **no formatter** — the bare message only: no timestamp, no level, no logger name
- **destination stderr** — which the plist now captures permanently

A `WARNING` carrying `BIFROST_API_KEY=sk-…` was confirmed to reach stderr **completely
unredacted**.

### Call-site audit — the whole surface is five lines

| site | payload | emitted today? | risk |
|---|---|---|---|
| `voice/io.py:119` | one float (seconds) | yes (WARNING) | none |
| `voice/io.py:136` | two ints (char counts) | yes (WARNING) | none |
| `voice/live.py:234` | `exc_info` from `_on_wake` | yes (ERROR) | **exception message + traceback** |
| `session/collector.py:123` | `str(OSError)` | yes (WARNING) | file paths (project names) |
| `voice/io.py:159` | `transcript[:60]` — **raw user speech** | **no** (INFO, dropped) | **latent** |

Two things follow that the security review did not have:

1. **Today's exposure is narrower than "logs leak secrets".** The two `io.py` WARNINGs are
   numeric. `collector.py` catches `OSError` *specifically*, so its string is an errno and a
   path — the "no raw content" comment there is accurate. Real exposure today is exception
   messages, tracebacks, and `~/.claude/projects/<project>` paths.
2. **The dangerous one is dormant, not absent.** `io.py:159` logs 60 characters of raw
   transcript. It is invisible only because nobody has configured a handler. **Configuring
   logging — which the LaunchAgent makes an obvious next step — switches this leak on.**

## The two problems

Conflating these is why "just redact the logs" is the wrong frame.

```
  PROBLEM A — credentials                    PROBLEM B — user content
  ─────────────────────────────              ──────────────────────────────
  API keys, connection URIs,                 speech, business context,
  PEM blocks, provider tokens                project names, file paths
  have a recognisable SHAPE                  have NO shape — arbitrary text
  ⇒ pattern redaction works                  ⇒ pattern redaction CANNOT work
  ⇒ fix: redacting Formatter                 ⇒ fix: do not log it
```

Measured against `sanitize_text`:

| input | result |
|---|---|
| `BIFROST_API_KEY=sk-abc…` | `BIFROST_API_KEY: [REDACTED]` |
| `postgres://user:hunter2@db/prod` | `postgres://user:[REDACTED]@db/prod` |
| `suppressed self-echo 'my bank pin is four nine two one'` | **passes through** |
| `suppressed self-echo 'tell Sarah the acquisition closes Friday'` | **passes through** |
| `[Errno 2] /Users/…/.claude/projects/patterninc-secretdeal/x.jsonl` | **passes through** |

No amount of tuning fixes the bottom three. They are not malformed secrets; they are ordinary
sentences. **Piping user content through `sanitize_text` and calling it safe would be the
dangerous outcome of this work** — it produces a log that *looks* sanitised.

## Problem A — evaluated approaches

Three leak vectors must all be covered: lazy `%` **args**, the literal **msg**, and rendered
**`exc_info`**. Prototyped and measured all three:

| approach | args | msg | traceback | leaks |
|---|:--:|:--:|:--:|---|
| none | ✗ | ✗ | ✗ | 3 |
| `logging.Filter` | ✓ | ✓ | **✗** | **1** |
| `logging.Formatter` | ✓ | ✓ | ✓ | **0** |

**A `Filter` is the intuitive choice and it is wrong.** Filters run *before* the handler renders
`exc_info`, so a filter cannot touch tracebacks — and an exception message is precisely where a
credential ends up (`ValueError: auth failed for sk-…`). Verified: the filter prototype still
emitted the secret once, from the traceback.

**Recommendation: a `RedactingFormatter` subclass**, four lines:

```python
class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return sanitize_text(super().format(record))
```

`super().format()` has already merged msg, args, `exc_info` and `stack_info` into one string, so
one `sanitize_text` call covers every vector. Cost is not a concern: **5 µs** on a short record,
131 µs on 2 KB, against a log volume of a handful of records per session.

### It needs a handler to attach to — which is the second half of the fix

There is nowhere to put a formatter today. That turns out to be convenient rather than
awkward: the daemon **should** configure logging anyway. Under launchd the current output has no
timestamp, no level and no logger name, which makes `edithd.err.log` nearly useless for the
"why did it crash-loop at 3am" question `deploy/README.md` exists to answer.

So one change buys both: configure logging in the composition root, with the redacting formatter
attached. Attach it at the **root** logger, not `edith.*` — third-party libraries (`httpx`,
`keyring`, `kuzu`) log too, and they are outside our call-site audit.

## Problem B — user content

Redaction is not applicable. Options, in the order I would take them:

1. **Do not log the content.** `io.py:159` wants to record *that* an echo was suppressed, not
   *what* was said. `_log.info("voice: suppressed self-echo (%d chars)", len(transcript))`
   keeps the diagnostic and removes the payload. This is the whole fix for the one live site.
2. **A lint rule for new sites.** The audit above is five lines today and was cheap; it will not
   stay cheap. Something that flags a log call whose args include a transcript/utterance/summary
   variable would keep the property from rotting. Unclear whether this is worth the machinery.
3. **Not recommended: a "privacy mode" that redacts all quoted strings.** Produces logs that
   look sanitised while guaranteeing nothing, which is worse than a log everyone knows is
   sensitive.

## Recommendation

1. **`RedactingFormatter` + root logging config in the composition root.** Closes Problem A
   completely, and fixes the unformatted-log problem the LaunchAgent created. One PR.
2. **Change `io.py:159` to log a length, not the transcript.** One line, removes the only live
   Problem-B site before a future handler switches it on.
3. **Keep `~/.edith` at mode 700 and say plainly in `deploy/README.md` that logs are sensitive.**
   Already done this session. Even with (1) and (2), logs will contain project names and file
   paths. The honest posture is "sensitive, protected by filesystem permissions", not "safe".
4. **Rotation is a separate concern.** `newsyslog.d` or `RotateLogs`. Unbounded growth for an
   always-on daemon is real but is not a leak; do not bundle it into this.

## Out of scope / deliberately not proposed

- **Structured (JSON) logging.** Would make redaction per-field rather than per-line, which is
  better in principle. Not worth it at five call sites.
- **Piping user content through `sanitize_text`.** See Problem B — actively harmful.
- **A `logging.Filter`.** Documented above as the plausible-but-wrong answer, recorded here so
  the next person does not re-derive it.
- **Encrypting the log directory.** The encrypted-volume TODO in `securestore.py` would cover
  logs too if it ever lands; not a reason to delay 700.

## Verification notes

Every claim above was measured in-process against the real `sanitize_text` on
`master` @ `3fed3ac`, not inferred: the `lastResort` level and its unredacted output, the
per-approach leak counts across the three vectors, the µs timings, and the credential-vs-speech
table. The call-site audit is a `grep` of every `_log.`/`logger.` use in `edith/`.
