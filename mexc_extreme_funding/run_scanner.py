from __future__ import annotations

import argparse
import time

from mexc_extreme_funding.config import DEFAULT_CONFIG
from mexc_extreme_funding.scanner import scan_once


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the independent MEXC extreme-funding scanner.")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--interval", type=float, default=DEFAULT_CONFIG.scanner_interval_seconds)
    args = parser.parse_args()
    while True:
        result = scan_once(DEFAULT_CONFIG)
        print(
            f"MEXC snapshots={result['snapshots']} eligible={result['eligible']} "
            f"opportunities={result['opportunities']} errors={len(result['errors'])} "
            f"settlement_comparisons={result['comparisons']} path={result['path']}",
            flush=True,
        )
        if not args.loop:
            return
        time.sleep(max(args.interval, 1.0))


if __name__ == "__main__":
    main()
