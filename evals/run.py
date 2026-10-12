"""Run the evaluation suite: `python -m evals.run` (see the package docstring)."""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from typing import List

from evals.checks import CHECKS
from src.l1_agent.utils.logging import setup_logging


async def _run(selected: List[str]) -> int:
    failed = 0
    for name, check in CHECKS.items():
        if selected and not any(word in name for word in selected):
            continue
        started = time.perf_counter()
        failures = await check()
        took = time.perf_counter() - started
        print(f"{'FAIL' if failures else 'ok  '}  {name}  ({took:.1f}s)")
        for failure in failures[:25]:
            print(f"        {failure}")
        failed += bool(failures)
    print(f"\n{'FAILED' if failed else 'All checks passed'}")
    return 1 if failed else 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluation suite for model use")
    parser.add_argument("only", nargs="*", help="run the checks whose name contains a word")
    parser.add_argument(
        "--live", action="store_true", help="also run against the provider in the environment"
    )
    args = parser.parse_args()
    setup_logging("CRITICAL")
    code = asyncio.run(_run(args.only))
    if args.live:
        from evals.live import run_live

        code = asyncio.run(run_live()) or code
    sys.exit(code)


if __name__ == "__main__":
    main()
