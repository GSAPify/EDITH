"""SageMaker entry point: train the custom "hey edith" openWakeWord model.

Runs INSIDE a SageMaker PyTorch GPU training container. Faithfully replicates
openWakeWord's automatic_model_training notebook as a script, with every fix the
Colab run surfaced baked in so we don't re-debug per job:

  - torch_audiomentations 0.11 calls torchaudio.set_audio_backend(), removed in
    torchaudio 2.x -> patched to a no-op (found via find_spec, NOT import, since
    importing the package is what crashes).
  - os.makedirs(..., exist_ok=True) everywhere (the notebook FileExistsError on rerun).
  - install `onnx` for the ONNX export; SKIP tensorflow/onnx_tf/tflite entirely
    (we only need the .onnx).
  - all heavy work on the /opt/ml volume (100 GB), not the small root fs.

Output: copies my_custom_model/hey_edith.onnx -> /opt/ml/model/ , which SageMaker
uploads to S3 as model.tar.gz. NOT run on real hardware by me — this is the
owner's GPU training run; expect 1-2 CloudWatch iterations.
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys

WORK = "/opt/ml/work"
MODEL_OUT = "/opt/ml/model"
OWW = os.path.join(WORK, "openwakeword")
PSG = os.path.join(WORK, "piper-sample-generator")


def sh(cmd: str, check: bool = True) -> None:
    print(f"\n+ {cmd}", flush=True)
    subprocess.run(cmd, shell=True, check=check)


def install_deps() -> None:
    """Mirror the notebook's install cell, minus tensorflow/onnx_tf; add onnx."""
    sh("pip install -q webrtcvad piper-phonemize || pip install -q webrtcvad", check=False)
    sh(f"git clone -q https://github.com/rhasspy/piper-sample-generator {PSG} || true", check=False)
    sh(
        f"wget -q -O {PSG}/models/en_US-libritts_r-medium.pt "
        "https://github.com/rhasspy/piper-sample-generator/releases/download/v2.0.0/en_US-libritts_r-medium.pt"
    )
    sh(f"git clone -q https://github.com/dscripka/openwakeword {OWW} || true", check=False)
    sh(f"pip install -q -e {OWW}")
    for pkg in (
        "piper-tts==1.4.2",  # provides the `piper` module v2 generate_samples imports
        "mutagen==1.47.0", "torchinfo==1.8.0", "torchmetrics==1.2.0",
        "speechbrain==0.5.14", "audiomentations==0.33.0", "torch-audiomentations==0.11.0",
        "acoustics==0.2.6", "pronouncing==0.2.0", "datasets==2.14.6",
        "deep-phonemizer==0.0.19", "onnx",
    ):
        sh(f"pip install -q {pkg}")

    # openWakeWord shared feature models (Colab workaround; exist_ok on the dir).
    res = os.path.join(OWW, "openwakeword", "resources", "models")
    os.makedirs(res, exist_ok=True)
    base = "https://github.com/dscripka/openWakeWord/releases/download/v0.5.1"
    for f in ("embedding_model.onnx", "embedding_model.tflite",
              "melspectrogram.onnx", "melspectrogram.tflite"):
        sh(f"wget -q {base}/{f} -O {os.path.join(res, f)}")


def write_generate_samples_shim() -> None:
    """Compat shim for openWakeWord train.py <-> piper-sample-generator v2.

    train.py does ``sys.path.insert(0, psg_path); from generate_samples import
    generate_samples`` (the OLD top-level module) and calls it WITHOUT ``model=``.
    v2 moved that function into the ``piper_sample_generator`` package and made
    ``model`` required. Drop a ``generate_samples.py`` at the repo root that
    re-exports v2's function with the bundled LibriTTS-R generator as the default
    model, so train.py's model-less calls work unchanged.
    """
    shim = os.path.join(PSG, "generate_samples.py")
    with open(shim, "w") as f:
        f.write(
            "import os\n"
            "from piper_sample_generator.__main__ import generate_samples as _gen\n"
            "_MODEL = os.path.join(os.path.dirname(__file__), 'models', "
            "'en_US-libritts_r-medium.pt')\n"
            "def generate_samples(*a, **k):\n"
            "    k.setdefault('model', _MODEL)\n"
            "    return _gen(*a, **k)\n"
        )
    print(f"wrote compat shim {shim}", flush=True)


def patch_torch_audiomentations() -> None:
    """Neutralize torchaudio.set_audio_backend() (removed in torchaudio 2.x).

    Locate the file via find_spec (does NOT execute the module — importing it is
    what raises AttributeError), then rewrite the dead call to ``pass``.
    """
    spec = importlib.util.find_spec("torch_audiomentations")
    if spec is None or not spec.submodule_search_locations:
        print("torch_audiomentations not found; skip patch", flush=True)
        return
    io_py = os.path.join(spec.submodule_search_locations[0], "utils", "io.py")
    src = open(io_py).read()
    patched = re.sub(r"torchaudio\.set_audio_backend\([^)]*\)", "pass", src)
    open(io_py, "w").write(patched)
    print(f"patched {io_py}", flush=True)


def download_data() -> None:
    """RIRs + background (audioset, fma) + precomputed negatives + validation."""
    import numpy as np
    import scipy.io.wavfile
    from datasets import Audio, load_dataset
    from tqdm import tqdm

    # MIT room impulse responses
    os.makedirs("mit_rirs", exist_ok=True)
    rir = load_dataset("davidscripka/MIT_environmental_impulse_responses",
                       split="train", streaming=True)
    for row in tqdm(rir, desc="rirs"):
        name = row["audio"]["path"].split("/")[-1]
        scipy.io.wavfile.write(os.path.join("mit_rirs", name), 16000,
                               (row["audio"]["array"] * 32767).astype(np.int16))

    # NOTE: AudioSet (agkphysics/AudioSet) is gated on HF now → raw wget 403s
    # (killed run #1). It's only extra background-noise diversity; FMA music +
    # RIRs are sufficient for a v1 model, so we skip AudioSet entirely.

    # FMA (1 hour of music) -> 16 kHz wav  (the reliable, ungated background source)
    os.makedirs("fma", exist_ok=True)
    fma = iter(load_dataset("rudraml/fma", name="small", split="train", streaming=True)
               .cast_column("audio", Audio(sampling_rate=16000)))
    for _ in tqdm(range(1 * 3600 // 30), desc="fma"):
        row = next(fma)
        name = row["audio"]["path"].split("/")[-1].replace(".mp3", ".wav")
        scipy.io.wavfile.write(os.path.join("fma", name), 16000,
                               (row["audio"]["array"] * 32767).astype(np.int16))

    # precomputed negative features (~16 GB) + validation set
    sh("wget -q https://huggingface.co/datasets/davidscripka/openwakeword_features/"
       "resolve/main/openwakeword_features_ACAV100M_2000_hrs_16bit.npy")
    sh("wget -q https://huggingface.co/datasets/davidscripka/openwakeword_features/"
       "resolve/main/validation_set_features.npy")


def write_config() -> str:
    import yaml
    cfg = yaml.safe_load(open(os.path.join(OWW, "examples", "custom_model.yml")).read())
    cfg["target_phrase"] = ["hey edith"]
    cfg["model_name"] = "hey_edith"
    # Env-overridable so a tiny diagnostic run (EDITH_N_SAMPLES=200 etc.) is cheap;
    # full-run defaults otherwise.
    cfg["n_samples"] = int(os.environ.get("EDITH_N_SAMPLES", "10000"))
    cfg["n_samples_val"] = int(os.environ.get("EDITH_N_SAMPLES_VAL", "1000"))
    cfg["steps"] = int(os.environ.get("EDITH_STEPS", "20000"))
    cfg["target_accuracy"] = 0.77
    cfg["target_recall"] = 0.5
    cfg["piper_sample_generator_path"] = PSG
    cfg["output_dir"] = os.path.join(WORK, "my_custom_model")
    cfg["rir_paths"] = [os.path.join(WORK, "mit_rirs")]
    cfg["background_paths"] = [os.path.join(WORK, "fma")]
    cfg["false_positive_validation_data_path"] = os.path.join(WORK, "validation_set_features.npy")
    cfg["feature_data_files"] = {
        "ACAV100M_sample": os.path.join(WORK, "openwakeword_features_ACAV100M_2000_hrs_16bit.npy")
    }
    cfg["custom_negative_phrases"] = [
        "hey edwin", "hey eddie", "hey enid", "heavy edit", "he said it", "hey it is",
    ]
    path = os.path.join(WORK, "my_model.yaml")
    with open(path, "w") as f:
        yaml.dump(cfg, f)
    print("wrote config:", cfg["target_phrase"], "->", cfg["model_name"], flush=True)
    return path


def resample_generated_clips(sr_target: int = 16000) -> None:
    """The v2 LibriTTS-R generator emits 22050 Hz clips, but openWakeWord's
    ``augment_clips`` requires 16 kHz ("Clip does not have the correct sample
    rate!"). Resample every generated clip in place, between generate and augment.
    """
    import glob
    from math import gcd

    import numpy as np
    import scipy.io.wavfile as wavio
    from scipy.signal import resample_poly

    base = os.path.join(WORK, "my_custom_model", "hey_edith")
    n = 0
    for sub in ("positive_train", "positive_test", "negative_train", "negative_test"):
        for path in glob.glob(os.path.join(base, sub, "*.wav")):
            sr, data = wavio.read(path)
            if sr == sr_target:
                continue
            g = gcd(sr_target, sr)
            y = resample_poly(data.astype(np.float32), sr_target // g, sr // g)
            wavio.write(path, sr_target, np.clip(y, -32768, 32767).astype(np.int16))
            n += 1
    print(f"resampled {n} clips -> {sr_target} Hz", flush=True)


def train(config_path: str) -> None:
    trainer = os.path.join(OWW, "openwakeword", "train.py")
    sh(f"{sys.executable} {trainer} --training_config {config_path} --generate_clips")
    resample_generated_clips()  # 22050 Hz (LibriTTS-R) -> 16 kHz (openWakeWord requires)
    sh(f"{sys.executable} {trainer} --training_config {config_path} --augment_clips")
    # train.py saves hey_edith.onnx, THEN attempts an optional .tflite conversion
    # that imports onnx_tf (which we intentionally don't install) and crashes.
    # Tolerate that non-zero exit — the .onnx is already written; main() verifies it.
    sh(f"{sys.executable} {trainer} --training_config {config_path} --train_model", check=False)


def main() -> None:
    import faulthandler
    faulthandler.enable()  # dump a stack on SIGSEGV/SIGABRT (the no-traceback hard-kill)
    os.makedirs(WORK, exist_ok=True)
    os.makedirs(MODEL_OUT, exist_ok=True)
    os.chdir(WORK)
    install_deps()
    write_generate_samples_shim()
    patch_torch_audiomentations()
    download_data()
    config_path = write_config()
    train(config_path)

    produced = os.path.join(WORK, "my_custom_model", "hey_edith.onnx")
    if not os.path.exists(produced):
        raise SystemExit(f"training finished but {produced} was not created")
    shutil.copy(produced, os.path.join(MODEL_OUT, "hey_edith.onnx"))
    print(f"SUCCESS: copied hey_edith.onnx -> {MODEL_OUT}", flush=True)


if __name__ == "__main__":
    main()
