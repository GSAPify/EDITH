"""SessionBus — raw transcript records → normalized ``session.event`` / ``session.state``.

Spec 04 §Step 2. SessionBus is the *logic* half of the collector: it takes one raw
Claude Code / OMC transcript record (a parsed JSONL dict, shape confirmed by the Step-0
spike) and:

  1. normalizes it → session_id, kind, state, a one-line summary, best-effort repo,
  2. **redacts** every free-text field it extracts (spec §Autonomy & secrets — terminal
     content is the highest-risk surface; nothing raw ever reaches the bus, a log, a model,
     or Memory),
  3. publishes ``session.event`` (discrete activity) and ``session.state`` (current snapshot),
  4. keeps an in-memory ``session_states`` map (only redacted summaries) and updates the
     Control API's ``last_event``.

It is deliberately stateless at the bus boundary except that map. It never touches disk.
The file/watchdog I/O lives in ``collector.py``; feeding SessionBus synthetic records makes
the whole classification + redaction path headlessly testable.

**kind** ∈ {start, prompt, tool_use, error, stop} — the discrete ``session.event`` classes.
**state** ∈ {working, waiting, error, idle} — the ``session.state`` snapshot. A record may
refresh state without emitting a discrete event (``kind is None``): e.g. an assistant text
reply keeps the session "working" but is not itself a narratable event.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from edith.bus import EventBus
from edith.memory.secrets import sanitize_text

# UI-only / bookkeeping record types the tap sees but that carry no session activity.
_IGNORED_TYPES = frozenset(
    {"mode", "ai-title", "last-prompt", "pr-link", "permission-mode",
     "file-history-snapshot", "queue-operation", "attachment"}
)

_SUMMARY_CAP = 160  # one-line summaries; keep bus payloads (and TTS) tight.

# A ``[REDACTED]`` marker sliced by the summary cap leaves a dangling fragment
# (``[REDACTE``, ``[RED`` …). Anchored on ``[R`` so it can't eat ordinary words.
_PARTIAL_MARKER = re.compile(r"\[R[EDACT]*$")


class _RuntimeStateLike(Protocol):
    last_event: str | None


@dataclass(frozen=True)
class _Norm:
    """A normalized record. ``kind is None`` → refresh state, emit no discrete event."""

    session_id: str
    state: str
    kind: str | None
    summary: str
    current_action: str | None
    repo: str | None


class SessionBus:
    """Normalize + classify + redact raw transcript records onto the internal bus."""

    def __init__(
        self,
        bus: EventBus,
        *,
        redactor: Callable[[str], str] = sanitize_text,
        runtime_state: _RuntimeStateLike | None = None,
    ) -> None:
        self._bus = bus
        self._redact = redactor
        self._runtime_state = runtime_state
        self._states: dict[str, dict[str, object]] = {}

    def session_states(self) -> dict[str, dict[str, object]]:
        """A copy of the current per-session state map (redacted summaries only)."""
        return {sid: dict(st) for sid, st in self._states.items()}

    async def ingest(self, record: dict) -> None:
        """Normalize one raw transcript record and publish its event/state."""
        norm = self._normalize(record)
        if norm is None:
            return

        # First time we see a session → synthesize a ``start`` before its first
        # real event, so a session that was already running when EDITH came up
        # still announces itself (spec killer-demo: "Session started in <repo>").
        if norm.session_id not in self._states:
            start = _Norm(
                session_id=norm.session_id,
                state="working",
                kind="start",
                summary=f"session started in {norm.repo or 'an unknown repo'}",
                current_action=None,
                repo=norm.repo,
            )
            # Seed the map so we don't recurse into another synthetic start.
            self._states[norm.session_id] = {}
            await self._publish(start)

        await self._publish(norm)

    async def _publish(self, norm: _Norm) -> None:
        summary = self._safe(norm.summary)
        current_action = self._safe(norm.current_action) if norm.current_action else None

        state_payload: dict[str, object] = {
            "session_id": norm.session_id,
            "state": norm.state,
            "current_action": current_action,
            "repo": norm.repo,
        }
        self._states[norm.session_id] = state_payload

        if norm.kind is not None:
            await self._bus.publish(
                "session.event",
                source="session_bus",
                payload={
                    "session_id": norm.session_id,
                    "kind": norm.kind,
                    "summary": summary,
                    "repo": norm.repo,
                },
            )
            if self._runtime_state is not None:
                short = norm.session_id[:8]
                self._runtime_state.last_event = self._safe(
                    f"[{short}] {norm.kind}: {summary}"
                )

        await self._bus.publish("session.state", source="session_bus", payload=state_payload)

    def _safe(self, text: str) -> str:
        """Redact, then cap. The one place free text becomes a publishable summary.

        Redaction runs on the FULL text before any truncation (so a secret can never
        survive by straddling the cap). If the cap then slices a ``[REDACTED]`` marker,
        the dangling fragment is dropped so no half-marker reaches the bus.
        """
        redacted = self._redact(text)
        if len(redacted) <= _SUMMARY_CAP:
            return redacted
        return _PARTIAL_MARKER.sub("", redacted[:_SUMMARY_CAP]).rstrip()

    def _normalize(self, record: dict) -> _Norm | None:
        rtype = record.get("type")
        if rtype in _IGNORED_TYPES:
            return None
        session_id = record.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            return None
        repo = _repo_of(record)

        if rtype == "user":
            return self._normalize_user(record, session_id, repo)
        if rtype == "assistant":
            return self._normalize_assistant(record, session_id, repo)
        if rtype == "system":
            return self._normalize_system(record, session_id, repo)
        return None

    def _normalize_user(self, record: dict, session_id: str, repo: str | None) -> _Norm | None:
        blocks = _content_blocks(record)
        # A tool_result carried on a user record = OMC's activity result, not an
        # owner prompt. Only the error case is a narratable event.
        for b in blocks:
            if b.get("type") == "tool_result":
                if b.get("is_error"):
                    return _Norm(session_id, "error", "error", "a tool call failed", None, repo)
                return _Norm(session_id, "working", None, "tool result", None, repo)

        # Otherwise it's an owner prompt/paste (the paste-capture path from the spike).
        # Only genuinely owner-originated prompts count; skill/system-injected text
        # (promptSource None/"system") is not owner activity.
        if record.get("promptSource") not in ("typed", "paste", "pasted"):
            return None
        text = _text_of(blocks) or _text_of_str(record)
        summary = f"owner: {text}" if text else "owner prompt"
        return _Norm(session_id, "working", "prompt", summary, summary, repo)

    def _normalize_assistant(
        self, record: dict, session_id: str, repo: str | None
    ) -> _Norm | None:
        for b in _content_blocks(record):
            if b.get("type") == "tool_use":
                name = str(b.get("name") or "a tool")
                return _Norm(
                    session_id, "working", "tool_use", f"using {name}", f"using {name}", repo
                )
        # Assistant text reply: keep the session "working" but emit no discrete event.
        return _Norm(session_id, "working", None, "responding", "responding", repo)

    def _normalize_system(
        self, record: dict, session_id: str, repo: str | None
    ) -> _Norm | None:
        if record.get("stopReason"):
            return _Norm(session_id, "idle", "stop", "session stopped", None, repo)
        if record.get("level") == "error" or record.get("hookErrors"):
            return _Norm(session_id, "error", "error", "a hook/tool errored", None, repo)
        return None


def _content_blocks(record: dict) -> list[dict]:
    msg = record.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def _text_of(blocks: list[dict]) -> str:
    return " ".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()


def _text_of_str(record: dict) -> str:
    msg = record.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    return content.strip() if isinstance(content, str) else ""


def _repo_of(record: dict) -> str | None:
    """Best-effort repo name from cwd/gitBranch. Nullable by contract (spec §Data model)."""
    cwd = record.get("cwd")
    branch = record.get("gitBranch")
    if not isinstance(cwd, str) or not cwd:
        return None
    # A real repo checkout has a branch that isn't the detached/root HEAD sentinel,
    # or lives under the owner's clone root. Otherwise (e.g. cwd=/Users/x) → null.
    if "/gitstuff/" in cwd or (isinstance(branch, str) and branch not in ("", "HEAD")):
        name = os.path.basename(cwd.rstrip("/"))
        return name or None
    return None
