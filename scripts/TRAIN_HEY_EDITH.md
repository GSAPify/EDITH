# Training the "Hey EDITH" wake word

openWakeWord ships no `hey_edith` model, so the custom phrase must be trained. We
use openWakeWord's **official, Colab-GPU-tested** automatic-training notebook
(local CPU training on this Mac would take hours and needs tens of GB — the GPU
path is ~20 min on Colab's free tier and produces a better model).

The positive-sample synthesis was already validated locally (12 varied "hey
edith" clips at `~/.edith/train/hey_edith_samples/` — `afplay` one to confirm the
phrase). Colab regenerates thousands on GPU.

## Steps

1. **Open the notebook** (Colab, free GPU):
   https://colab.research.google.com/github/dscripka/openWakeWord/blob/main/notebooks/automatic_model_training.ipynb

2. **Runtime → Change runtime type → T4 GPU** (or any GPU).

3. In the **config cell**, change only these (leave the data-download cells as-is
   — they fetch the negatives / RIRs / AudioSet / validation set automatically):
   ```yaml
   target_phrase: ["hey edith"]
   model_name: "hey_edith"
   # optional, for a stronger model (slower): bump samples + steps
   n_samples: 5000
   n_samples_val: 500
   steps: 20000
   ```
   If the config cell exposes `custom_negative_phrases`, seed it with near-misses
   so it doesn't false-fire on them:
   ```yaml
   custom_negative_phrases: ["hey edwin", "hey eddie", "hey enid", "heavy edit", "he said it", "hey it is"]
   ```

4. **Runtime → Run all.** It will: synthesize positives (piper-sample-generator),
   download negatives + RIRs + background, train, and export. ~15–25 min on GPU.

5. **Download the result.** The notebook writes `my_custom_model/hey_edith.onnx`
   (and `.tflite`). Download the **`.onnx`** (Colab file browser → `my_custom_model/`
   → download, or the notebook's final `files.download(...)` cell).

6. **Drop it here** and tell me:
   ```
   mkdir -p ~/.edith/models
   mv ~/Downloads/hey_edith.onnx ~/.edith/models/hey_edith.onnx
   ```
   Then I set `EDITH_WAKE_MODEL=~/.edith/models/hey_edith.onnx` (the daemon wiring
   + the `python -m edith.voice` prompt already read this and will say "Hey Edith"),
   and load-verify the model (`openwakeword` loads it + `.predict` runs). Actual
   wake accuracy is yours to smoke — say "Hey EDITH" and watch it trigger.

## Notes
- The `.onnx` is small (~1–2 MB); once trained we can commit it so the wake word
  is reproducible, or keep it under `~/.edith/models/` (untracked). Your call.
- If it false-fires or misses, retrain with more `n_samples` / more
  `custom_negative_phrases` (the two knobs that matter most).
