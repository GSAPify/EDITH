"""Submit the hey_edith SageMaker training job (async) from the Mac.

Uses the `sandbox` profile + the edith exec role. Picks a PyTorch GPU DLC whose
torchaudio is 2.x (so the set_audio_backend patch in the entry script applies,
matching the Colab fix). Submits with wait=False so we monitor via
DescribeTrainingJob / CloudWatch. Prints the job name.

  python scripts/sagemaker/launch.py
"""

from __future__ import annotations

import os

import boto3
import sagemaker
from sagemaker.pytorch import PyTorch

REGION = "us-west-2"
PROFILE = "sandbox"
ROLE = "arn:aws:iam::847068433460:role/edith-sagemaker-exec-role"
HERE = os.path.dirname(os.path.abspath(__file__))


def main() -> None:
    boto_sess = boto3.Session(profile_name=PROFILE, region_name=REGION)
    sm_sess = sagemaker.Session(boto_session=boto_sess)

    instance = os.environ.get("EDITH_INSTANCE", "ml.g4dn.xlarge")
    # PYTHONFAULTHANDLER dumps a C-level stack on SIGSEGV/SIGABRT; pass through the
    # tiny-config overrides for a cheap diagnostic run.
    env = {"PYTHONUNBUFFERED": "1", "PYTHONFAULTHANDLER": "1"}
    for k in ("EDITH_N_SAMPLES", "EDITH_N_SAMPLES_VAL", "EDITH_STEPS"):
        if k in os.environ:
            env[k] = os.environ[k]

    est = PyTorch(
        entry_point="train_hey_edith.py",
        source_dir=HERE,
        role=ROLE,
        framework_version="2.3.0",
        py_version="py311",
        instance_type=instance,
        instance_count=1,
        volume_size=100,               # 16 GB negatives + generated clips need room
        max_run=3 * 60 * 60,           # 3h ceiling; job self-terminates on exit
        base_job_name="edith-heyedith",
        sagemaker_session=sm_sess,
        disable_profiler=True,
        environment=env,
    )
    print(f"instance={instance} env={ {k: v for k, v in env.items() if k.startswith('EDITH')} }")
    est.fit(wait=False)
    print("SUBMITTED_JOB:", est.latest_training_job.name)


if __name__ == "__main__":
    main()
