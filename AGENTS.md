## Learned User Preferences
- Keep models and classes at module scope; never define them inside functions.
- Do not use emoji in code, and follow existing naming, organization, and implementation patterns.
- For behavior changes, use focused TDD: capture a failing test before production edits, then run focused pytest, Pyright, and Ruff checks.
- Preserve unrelated working-tree changes; do not create branches or worktrees, commit, push, or open pull requests unless explicitly requested.

## Learned Workspace Facts
- EDITH targets Python 3.11 and uses the repository `.venv`; Pyright resolves it through `venvPath = "."` and `venv = ".venv"` in `pyproject.toml`.
- The shipping voice loop is half-duplex over `sounddevice`; VPIO, Speex, and AEC code remains experimental or benchmark-only and is not wired into `run_live_loop`.
- `VoiceIO.speak()` is non-arming for startup speech, session narration, and background-reasoning pings; direct owner replies and skill acknowledgements/final replies use `speak_response()`.
- Voice completion uses lock-protected per-speech records and a one-shot follow-up signal retained until all speech is idle; barge-in, abandonment, errors, cancellation, and pending-handle timeouts must not arm follow-up.
- Voice timing defaults to a 0.3-second cooldown, a ceiling-rounded residual flush of four 80 ms frames, and a 10-second follow-up window; `EDITH_SPEAK_FLUSH_SECONDS` remains configurable.
- Dynamic PyObjC boundaries stay localized: AppKit uses guarded runtime lookups, while AVFoundation accesses route through a narrow `Any` boundary without blanket ignores.
