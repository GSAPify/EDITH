"""CLI entrypoint: ``python -m edith.finder "your query"`` (spec 09 §CLI).

    python -m edith.finder "which repo handles PR reviews?" [--k N] [--data-dir PATH]

Prints the ranked repos found in the live Memory graph. If Bifrost env is
present (BIFROST_BASE_URL / BIFROST_API_KEY), it also prints a one-line Sonnet
summary of the top hits (``summarize_hits``); otherwise it prints ranking only.

Reads the store opened at ``--data-dir`` / ``EDITH_DATA_DIR``. The ranking is
model-free; the summary is the only path that touches Bifrost.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

import httpx

from edith.finder import find_repos, summarize_hits
from edith.memory.vector import VectorMemoryStore
from edith.router import Router, Tier

_DEFAULT_DATA_DIR = "~/.edith/data"


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m edith.finder", description=__doc__)
    p.add_argument("query", help="natural-language query over the ingested repos")
    p.add_argument("--k", type=int, default=5, help="number of repos to return")
    p.add_argument("--data-dir", default=os.environ.get("EDITH_DATA_DIR", _DEFAULT_DATA_DIR))
    p.add_argument("--max-tokens", type=int, default=256, help="Sonnet summary cap")
    return p


def _models() -> dict[Tier, str]:
    return {
        Tier.HAIKU: os.environ.get("BIFROST_MODEL_HAIKU", "claude-haiku-4-5-20251001"),
        Tier.SONNET: os.environ.get("BIFROST_MODEL_SONNET", "claude-sonnet-4-6"),
        Tier.OPUS: os.environ.get("BIFROST_MODEL_OPUS", "claude-opus-4-8"),
    }


async def _run(args: argparse.Namespace) -> int:
    data_dir = os.path.expanduser(args.data_dir)
    store = VectorMemoryStore(os.path.join(data_dir, "memory.kuzu"))
    try:
        hits = find_repos(args.query, store, k=args.k)
    finally:
        store.close()

    if not hits:
        print(f'No repos matched "{args.query}" in {data_dir}.')
        return 0

    print(f'Repos matching "{args.query}":')
    for i, hit in enumerate(hits, 1):
        summary = f" — {hit.summary}" if hit.summary else ""
        print(f"  {i}. {hit.name}  (score={hit.score:.3f}, degree={hit.degree}){summary}")

    base = os.environ.get("BIFROST_BASE_URL")
    key = os.environ.get("BIFROST_API_KEY")
    if base and key:
        async with httpx.AsyncClient(base_url=base, timeout=60.0) as client:
            router = Router(client=client, api_key=key, models=_models())
            answer = await summarize_hits(args.query, hits, router)
        print("\nEDITH:", answer)
    else:
        print("\n(set BIFROST_BASE_URL / BIFROST_API_KEY for a spoken summary)",
              file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
