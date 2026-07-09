#!/usr/bin/env python3
"""Deterministic wake-word diagnostic — NO microphone.

Synthesizes speech with macOS `say`, feeds it through openWakeWord exactly like the live
loop, and prints the peak score. Isolates model + runtime health from mic/pronunciation/
threshold. `alexa` (a bundled reference model) is the runtime control: if alexa scores ~1.0
on "alexa" but hey_edith scores low on "hey edith", the runtime is fine and the trained model
just has low recall (raise sensitivity / retrain). If even alexa is ~0, the runtime regressed.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import wave

import numpy as np
from openwakeword.model import Model

FRAME = 1280
RATE = 16000


def say_wav(text: str) -> str:
    path = os.path.join(tempfile.gettempdir(), f"diag_{abs(hash(text))}.wav")
    subprocess.run(
        ["say", "-o", path, "--data-format=LEI16@16000", text],
        check=True, capture_output=True,
    )
    return path


def read_int16(path: str) -> np.ndarray:
    with wave.open(path, "rb") as w:
        frames = w.readframes(w.getnframes())
        return np.frombuffer(frames, dtype=np.int16)


def peak_score(model: Model, pcm: np.ndarray) -> float:
    model.reset()
    peak = 0.0
    for i in range(0, len(pcm) - FRAME, FRAME):
        scores = model.predict(pcm[i : i + FRAME])
        if isinstance(scores, dict) and scores:
            peak = max(peak, max(float(v) for v in scores.values()))
    return peak


def main() -> None:
    hey_edith = os.path.expanduser(os.environ.get(
        "EDITH_WAKE_MODEL", "~/.edith/models/hey_edith.onnx"))

    print(f"model: {hey_edith}")
    edith_model = Model(wakeword_models=[hey_edith])
    alexa_model = Model(wakeword_models=["alexa"])  # bundled reference / runtime control

    cases = [
        (edith_model, "hey edith", "hey edith"),
        (edith_model, "hey edith", "hey, edith"),
        (edith_model, "hey edith", "hey eadith"),
        (edith_model, "silence-ish", "the weather is nice today"),
        (alexa_model, "alexa (CONTROL)", "alexa"),
        (alexa_model, "alexa (CONTROL)", "the weather is nice today"),
    ]
    print(f"\n{'model':22} {'said':30} peak_score")
    print("-" * 66)
    for model, label, text in cases:
        pcm = read_int16(say_wav(text))
        score = peak_score(model, pcm)
        flag = "  <-- would WAKE @0.5" if score >= 0.5 else ""
        print(f"{label:22} {text!r:30} {score:.3f}{flag}")


if __name__ == "__main__":
    main()
