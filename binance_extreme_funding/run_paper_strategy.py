from __future__ import annotations

import argparse
import time

from binance_extreme_funding.config import DEFAULT_CONFIG
from binance_extreme_funding.paper_strategy import run_paper_strategy_once


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Binance extreme-funding paper strategy.")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--interval", type=float, default=DEFAULT_CONFIG.strategy_interval_seconds)
    args = parser.parse_args()
    while True:
        cycle_started = time.monotonic()
        result = run_paper_strategy_once(DEFAULT_CONFIG)
        print("Binance paper " + " ".join(f"{key}={value}" for key, value in result.items()), flush=True)
        if not args.loop:
            return
        elapsed = time.monotonic() - cycle_started
        time.sleep(max(args.interval - elapsed, 1.0))


if __name__ == "__main__":
    main()
