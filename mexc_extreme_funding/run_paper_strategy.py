from __future__ import annotations

import argparse
import time

from mexc_extreme_funding.config import DEFAULT_CONFIG
from mexc_extreme_funding.paper_strategy import run_paper_strategy_once


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the MEXC extreme-funding paper strategy.")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--interval", type=float, default=DEFAULT_CONFIG.strategy_interval_seconds)
    args = parser.parse_args()
    while True:
        result = run_paper_strategy_once(DEFAULT_CONFIG)
        print("MEXC paper " + " ".join(f"{key}={value}" for key, value in result.items()), flush=True)
        if not args.loop:
            return
        time.sleep(max(args.interval, 1.0))


if __name__ == "__main__":
    main()
