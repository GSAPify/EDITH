# Running `edithd` as a launchd LaunchAgent

`python -m edith.daemon` (`edith/daemon/__main__.py`) is the composition root. Until now,
nothing started it but a human in a terminal — close the terminal and EDITH stops. This
directory ships the pieces that make it always-on:

- `com.gsapify.edithd.plist` — a launchd LaunchAgent **template**. Not auto-loaded by
  anything; contains no secrets.
- `edithd-launcher.sh` — a wrapper that sources `.env` (launchd does not) and then
  `exec`s the daemon.

## Read this before you install it: the Kuzu lock

**Verified from the code, not assumed** (`edith/daemon/edithd.py`, `edith/memory/store.py`):

`edithd` opens `MemoryStore` (embedded Kuzu, `~/.edith/data/memory.kuzu`) once at startup and
holds it for the process's entire life. Kuzu is single-process/single-writer — a second process
opening the same DB directory fails or corrupts state. The Kuzu handle is only released by
`MemoryStore.close()` (`store.py:391-394`, `self._conn.close(); self._db.close()`), and the ONLY
code path that calls it is `EdithDaemon.stop()` (`edithd.py:361-402`), which itself only runs on
a graceful shutdown: a Control API `kill` command or `Ctrl-C` (`KeyboardInterrupt`) in
`__main__.py`.

**Consequence:** once `edithd` is running always-on via launchd, `python -m edith.viewer`,
`edith.finder`, and `edith.ingest` will **fail to open the graph** — the lock is held. You must
stop `edithd` first (see "Escape hatch" below), run the other tool, then restart `edithd`.

### Does `pause` release the lock? No — verified from the code.

Read `control.py` / `state.py` / `edithd.py` end to end: the Control API `pause` command calls
`RuntimeState.pause()` (`state.py:48-51`), which only flips an enum
(`RUNNING`→`PAUSED`). It never touches `self._memory`. `EdithDaemon` reads `state.is_paused` only
to make Brain skip a turn's model-call-and-remember pass (`edithd.py:232`) — a privacy/behavior
switch, not a resource-release one. `compact()`/`close()` are called nowhere in the pause path.

**`pause` only suspends processing. It does NOT release the Kuzu handle.** The graph stays
locked to the paused `edithd` process. If you need the viewer/finder/ingest to open
`memory.kuzu`, `pause` is not sufficient — you must stop the daemon (see below).

### Escape hatch: stop the daemon to free the lock

This plist ships with `KeepAlive` as a bare `<true/>` (as specified) — launchd respawns the job
unconditionally on exit, graceful or not. That has a real consequence for the lock, verified from
the code:

- A Control API `kill` command (`ControlClient.send({"cmd": "kill"})`) runs the graceful path —
  `_on_kill` → `EdithDaemon.stop()` (`edithd.py:361-402`) → `compact()` then `MemoryStore.close()`
  → the process exits cleanly (`__main__._amain` returns 0). But because `KeepAlive` is
  unconditional, **launchd respawns edithd immediately**, which reopens `memory.kuzu` and
  re-takes the lock within moments. `kill` alone does not leave a window you can rely on to run
  the viewer/finder/ingest.
- The only way to actually free the lock for a useful window is to unload the job first, so
  `KeepAlive` cannot fire:

  ```
  launchctl bootout gui/$UID/com.gsapify.edithd
  ```

  Once the process has exited, `memory.kuzu` is free for `edith.viewer` / `edith.finder` /
  `edith.ingest` to open. Reload with `launchctl bootstrap` (see below) when you're done.

**Caveat, also verified from the code:** `edithd` installs no `SIGTERM`/`SIGINT` handler anywhere
(`grep -rn "add_signal_handler\|signal\." edith/` is empty). `launchctl bootout` delivers
`SIGTERM`, which is not the graceful `kill`-command path — `EdithDaemon.stop()` (and therefore
`MemoryStore.close()`/`compact()`) does not run; the process is killed by the OS default
disposition for `SIGTERM`. Whatever lock-release behavior follows from that is a property of
Kuzu's own file-locking (a third-party C++ library, out of this repo), not verified here.

Net effect, plainest terms: **`pause`** never releases the lock. **`kill`** releases it and then
launchd immediately re-takes it via `KeepAlive`. **`bootout`** is the only path that leaves the
lock free for another tool to use — at the cost of an ungraceful stop (no `compact()`, no clean
`close()`). There is currently no in-between option; a future item (e.g. a `KeepAlive` policy that
distinguishes intentional stop from crash) would need to change this plist, which is out of scope
here.

## Install

1. Make sure the repo has a working venv at `.venv` (`uv sync` from the repo root) and a
   filled-in `.env` (copy `.env.example`, fill in real values, **never commit it**).

   **Lock down the permissions — not committing it is not enough:**

   ```
   chmod 600 .env              # only you may read it
   chmod 700 ~/.edith          # logs + graph live here
   ```

   Why this is a hard requirement and not hygiene advice: `edithd-launcher.sh` **sources**
   `.env`, which *executes* it. A group- or world-**writable** `.env` is therefore arbitrary
   code running as you at every login. A world-**readable** one hands `BIFROST_API_KEY` to
   any other local account — on the machine this was built on, `.env` was `0644`, `~` was
   group-`staff` traversable, and a second account (uid 501) could read it outright.

   The launcher **refuses to start** unless `.env` is mode `600` and owned by you, so a chmod
   regression fails loudly at boot instead of quietly re-exposing the key. If it exits with
   `refusing to source …`, that check is doing its job.

   **Logs are NOT redacted.** `sanitize_text` is the choke-point for model/TTS/bus payloads;
   it does not run on log records. Python's `lastResort` handler sends WARNING+ to stderr,
   which the plist captures permanently — e.g. transcript-tail errors from
   `TranscriptCollector`. Treat `~/.edith/logs/` as sensitive and keep it `700`. They are also
   **not rotated** — launchd will not do it, so they grow unbounded for an always-on daemon;
   add a `newsyslog.d` entry if that matters to you.
2. Copy the plist template into place and substitute both placeholders with real absolute
   paths — `__EDITH_REPO_DIR__` (where you cloned this repo) and `__EDITH_HOME_DIR__` (your
   `$HOME`, since launchd does not expand `~`):

   ```
   mkdir -p ~/.edith/logs
   sed -e "s#__EDITH_REPO_DIR__#$HOME/gitstuff/EDITH#g" \
       -e "s#__EDITH_HOME_DIR__#$HOME#g" \
       deploy/com.gsapify.edithd.plist > ~/Library/LaunchAgents/com.gsapify.edithd.plist
   ```

   (Adjust `$HOME/gitstuff/EDITH` above if you cloned the repo somewhere else.)

3. Load it:

   ```
   launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.gsapify.edithd.plist
   ```

## Uninstall / stop

```
launchctl bootout gui/$UID/com.gsapify.edithd
rm ~/Library/LaunchAgents/com.gsapify.edithd.plist   # if you want it gone for good
```

## Checking status

```
launchctl print gui/$UID/com.gsapify.edithd
```

Look for `state = running` and a PID. Or use the Control API directly (the daemon exposes a
unix socket at `<data-dir>/edithd.sock`, e.g. via `edith.daemon.client.ControlClient`) and send
`{"cmd": "status"}`.

## Logs

`StandardOutPath` / `StandardErrorPath` in the plist point at:

```
~/.edith/logs/edithd.out.log
~/.edith/logs/edithd.err.log
```

(after you substitute `__EDITH_HOME_DIR__`). Tail them to confirm the daemon actually started
("`[edithd] running (...)`" on stdout) or to see why it didn't.

**A specific failure signature to know:** `python -m edith.daemon` always tries to build a real
`VoiceIO` (`_amain` in `__main__.py`) and exits with code 1 if that fails (e.g. the `[voice]`
extra isn't installed, or `portaudio` is missing). Under `KeepAlive: true` that turns into a tight
respawn loop, throttled to once per `ThrottleInterval` (10s) — `edithd.err.log` will show
`[edithd] cannot start the voice loop: ...` repeating every ~10 seconds. If you see that, install
the audio stack (`brew install portaudio && uv pip install -e '.[voice]'`) before relying on
launchd to keep it up.

## What was and wasn't verified here

`launchctl bootstrap`/`bootout` and the actual always-on behavior are **owner LIVE-SMOKE** —
they require a real launchd session and cannot be exercised headlessly in this environment. What
*was* verified:

- The plist template is well-formed XML and parses with `plistlib` (stdlib).
- It has the required keys (`Label`, `ProgramArguments`, `RunAtLoad`, `KeepAlive`, the two log
  path keys) and `Label` matches the filename.
- No credential-shaped value is present in the tracked template.
- The wrapper script passes `sh -n`/`bash -n` syntax checking.
- The Kuzu single-process lock behavior and the `pause` vs. `kill` distinction, by reading
  `edithd.py`, `store.py`, `control.py`, and `state.py` directly (cited above with line numbers).

None of the above proves the daemon actually launches correctly under real launchd — that step
is on the owner's machine.
