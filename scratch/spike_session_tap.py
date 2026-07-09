#!/usr/bin/env python3
"""Spike (spec 04 §Step 0): what can a tap actually see of a running OMC / Claude Code session?

THROWAWAY. Not tested, not imported by anything. It exists only to produce the written
finding in `spike_session_tap_findings.md`, which decides the SessionBus collector mechanism
for Slice 4 Step 1. Delete once the collector is built.

Run against the live machine:  python scratch/spike_session_tap.py

It answers, empirically, the spec's open questions:
  1. Candidate A (hooks): which Claude Code hooks are configured + fire?
  2. Candidate B (file tail): what do the session transcript JSONL files contain, and when?
  3. Killer sub-goal: does pasted terminal content land where a tap can read it?
  4. Latency: are records flushed per-event or batched at turn end?
  5. Session enumeration + repo detection: can we list sessions and name their repo?
"""

from __future__ import annotations

import collections
import glob
import json
import os
import re

PROJECTS = os.path.expanduser("~/.claude/projects")


def newest_transcript() -> str | None:
    files = glob.glob(os.path.join(PROJECTS, "*", "*.jsonl"))
    return max(files, key=os.path.getmtime) if files else None


def read_records(path: str):
    with open(path) as f:
        for line in f:
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def probe(path: str) -> None:
    types = collections.Counter()
    prompt_sources = collections.Counter()
    attachment_types = collections.Counter()
    tool_names: list[str] = []
    placeholder_pastes = 0
    inline_images = 0
    meta_sample: dict[str, dict] = {}

    for d in read_records(path):
        t = d.get("type")
        types[t] += 1
        if t in ("user", "assistant", "system") and t not in meta_sample:
            meta_sample[t] = {
                k: d.get(k) for k in ("sessionId", "cwd", "gitBranch", "timestamp", "version")
            }
        if t == "attachment":
            attachment_types[(d.get("attachment") or {}).get("type", "?")] += 1
        if t == "user":
            prompt_sources[d.get("promptSource")] += 1
        msg = d.get("message") or {}
        content = msg.get("content") if isinstance(msg, dict) else None
        blocks = content if isinstance(content, list) else []
        for b in blocks:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use" and len(tool_names) < 12:
                tool_names.append(b.get("name"))
            if b.get("type") == "image":
                inline_images += 1
            if b.get("type") == "text" and re.search(
                r"\[Pasted|\+\d+ lines|Pasted text", b.get("text", ""), re.I
            ):
                placeholder_pastes += 1

    print(f"transcript: {path}")
    print("record types      :", dict(types))
    print("user promptSource :", dict(prompt_sources))
    print("attachment .type  :", dict(attachment_types))
    print("tool_use names    :", tool_names)
    print("inline image blks :", inline_images)
    print("paste PLACEHOLDERS:", placeholder_pastes, "(0 => text pastes land inline)")
    print("per-type metadata :", json.dumps(meta_sample, indent=2))


if __name__ == "__main__":
    tr = newest_transcript()
    if not tr:
        raise SystemExit(f"no transcripts under {PROJECTS}")
    probe(tr)
