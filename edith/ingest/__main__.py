"""CLI entrypoint: ``python -m edith.ingest`` (spec 08 §Pipeline).

    python -m edith.ingest [--repos NAME ...] [--limit N] [--dry-run]
                           [--data-dir PATH] [--scan-root PATH]

Builds a real Router from the environment (BIFROST_BASE_URL / BIFROST_API_KEY /
BIFROST_MODEL_*) and runs the pipeline once, then prints the status report.
``--dry-run`` needs no Bifrost credentials (no model calls).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

import httpx

from edith.ingest.pipeline import run_ingest
from edith.router import Router, Tier

_DEFAULT_DATA_DIR = "~/.edith/data"
_DEFAULT_SCAN_ROOT = "~/gitstuff"


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m edith.ingest", description=__doc__)
    p.add_argument("--repos", nargs="*", default=None, help="only ingest these repo names")
    p.add_argument("--limit", type=int, default=None, help="cap number of repos")
    p.add_argument("--dry-run", action="store_true", help="no model calls, no writes")
    p.add_argument("--data-dir", default=os.environ.get("EDITH_DATA_DIR", _DEFAULT_DATA_DIR))
    p.add_argument("--scan-root", default=_DEFAULT_SCAN_ROOT)
    p.add_argument("--max-tokens", type=int, default=512, help="Opus deep-extract cap")
    return p


def _models() -> dict[Tier, str]:
    return {
        Tier.HAIKU: os.environ.get("BIFROST_MODEL_HAIKU", "claude-haiku-4-5-20251001"),
        Tier.SONNET: os.environ.get("BIFROST_MODEL_SONNET", "claude-sonnet-4-6"),
        Tier.OPUS: os.environ.get("BIFROST_MODEL_OPUS", "claude-opus-4-8"),
    }


async def _run(args: argparse.Namespace) -> int:
    if args.dry_run:
        report = await run_ingest(
            scan_root=args.scan_root,
            data_dir=args.data_dir,
            router=_NullRouter(),
            repos=args.repos,
            limit=args.limit,
            dry_run=True,
            deep_max_tokens=args.max_tokens,
        )
        print(report.render())
        return 0

    base = os.environ.get("BIFROST_BASE_URL")
    key = os.environ.get("BIFROST_API_KEY")
    if not base or not key:
        print("BIFROST_BASE_URL / BIFROST_API_KEY not set (needed unless --dry-run)",
              file=sys.stderr)
        return 2

    async with httpx.AsyncClient(base_url=base, timeout=60.0) as client:
        router = Router(client=client, api_key=key, models=_models())
        report = await run_ingest(
            scan_root=args.scan_root,
            data_dir=args.data_dir,
            router=router,
            repos=args.repos,
            limit=args.limit,
            deep_max_tokens=args.max_tokens,
        )
    print(report.render())
    return 0


class _NullRouter:
    """Placeholder Router for --dry-run; never called (no model calls)."""

    async def model_call(self, messages, tier_hint, max_tokens=1024):  # noqa: ANN001, ARG002
        raise RuntimeError("model_call must not run during --dry-run")


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
