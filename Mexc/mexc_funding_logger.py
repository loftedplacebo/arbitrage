#!/usr/bin/env python3
"""
Lightweight MEXC futures funding-rate logger.

Purpose:
    Capture repeated funding-rate snapshots so we can test whether MEXC funding
    is fixed/locked before settlement, like KuCoin, or whether it floats until
    settlement.

What it logs:
    - exchange
    - symbol
    - observed_at_utc
    - funding_rate
    - collect_cycle_hours
    - next_settle_time_utc
    - minutes_to_settle
    - idx_price
    - fair_price
    - raw exchange timestamp

Example usage:
    python mexc_funding_logger.py
    python mexc_funding_logger.py --symbols BTC_USDT ETH_USDT SOL_USDT
    python mexc_funding_logger.py --symbols BTC_USDT --once

Default behaviour:
    - Discovers all active MEXC USDT perpetual futures symbols.
    - Logs one snapshot every 30 minutes.
    - This is enough to compare the published funding rate before and after
      the funding settlement time.

Notes:
    - Read-only. No API keys required.
    - Keep polling light. MEXC docs currently state funding-rate endpoint limit
      is 20 requests per 2 seconds.
"""


from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import requests


EXCHANGE = "MEXC"
BASE_URL = "https://api.mexc.com"
FUNDING_ENDPOINT = "/api/v1/contract/funding_rate/{symbol}"
CONTRACT_DETAIL_ENDPOINT = "/api/v1/contract/detail"
DEFAULT_SYMBOLS = ["BTC_USDT", "ETH_USDT", "SOL_USDT", "XRP_USDT", "DOGE_USDT"]
DEFAULT_OUTPUT_DIR = Path("data") / "mexc"


class GracefulExit:
    """
    Simple stop flag.

    On Windows/PowerShell, explicit signal wiring can behave unexpectedly in
    some shells/editors. We keep this deliberately simple and rely on the
    KeyboardInterrupt handler in __main__ for Ctrl+C shutdown.
    """

    def __init__(self) -> None:
        self.stop = False


@dataclass
class FundingSnapshot:
    exchange: str
    symbol: str
    observed_at_utc: str
    funding_rate: Optional[float]
    funding_rate_pct: Optional[float]
    collect_cycle_hours: Optional[int]
    next_settle_time_ms: Optional[int]
    next_settle_time_utc: Optional[str]
    minutes_to_settle: Optional[float]
    idx_price: Optional[float]
    fair_price: Optional[float]
    exchange_timestamp_ms: Optional[int]
    raw_success: Optional[bool]
    raw_code: Optional[int]
    error: Optional[str] = None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def ms_to_utc_iso(ms: Optional[int]) -> Optional[str]:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def safe_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def safe_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def request_json(session: requests.Session, url: str, timeout: int = 10) -> Dict[str, Any]:
    response = session.get(url, timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError(f"Expected JSON object, got {type(payload).__name__}")
    return payload


def fetch_funding_rate(session: requests.Session, symbol: str) -> FundingSnapshot:
    observed = utc_now()
    url = f"{BASE_URL}{FUNDING_ENDPOINT.format(symbol=symbol)}"

    try:
        payload = request_json(session, url)
        data = payload.get("data") or {}
        if not isinstance(data, dict):
            raise ValueError(f"Unexpected data payload for {symbol}: {data!r}")

        funding_rate = safe_float(data.get("fundingRate"))
        next_settle_ms = safe_int(data.get("nextSettleTime"))
        minutes_to_settle = None
        if next_settle_ms is not None:
            minutes_to_settle = round((next_settle_ms / 1000 - observed.timestamp()) / 60, 4)

        return FundingSnapshot(
            exchange=EXCHANGE,
            symbol=str(data.get("symbol") or symbol),
            observed_at_utc=observed.isoformat(),
            funding_rate=funding_rate,
            funding_rate_pct=round(funding_rate * 100, 8) if funding_rate is not None else None,
            collect_cycle_hours=safe_int(data.get("collectCycle")),
            next_settle_time_ms=next_settle_ms,
            next_settle_time_utc=ms_to_utc_iso(next_settle_ms),
            minutes_to_settle=minutes_to_settle,
            idx_price=safe_float(data.get("idxPrice")),
            fair_price=safe_float(data.get("fairPrice")),
            exchange_timestamp_ms=safe_int(data.get("timestamp")),
            raw_success=payload.get("success"),
            raw_code=safe_int(payload.get("code")),
            error=None,
        )

    except Exception as exc:  # deliberately broad so logger keeps running
        logging.exception("Failed to fetch funding rate for %s", symbol)
        return FundingSnapshot(
            exchange=EXCHANGE,
            symbol=symbol,
            observed_at_utc=observed.isoformat(),
            funding_rate=None,
            funding_rate_pct=None,
            collect_cycle_hours=None,
            next_settle_time_ms=None,
            next_settle_time_utc=None,
            minutes_to_settle=None,
            idx_price=None,
            fair_price=None,
            exchange_timestamp_ms=None,
            raw_success=None,
            raw_code=None,
            error=str(exc),
        )


def discover_usdt_perp_symbols(session: requests.Session, top: int = 0) -> List[str]:
    """
    Pull contract metadata and return active USDT perpetual symbols where possible.

    MEXC's detail endpoint can be called without a symbol and returns many
    contracts. The response shape has changed historically, so this parser is
    intentionally defensive.
    """
    url = f"{BASE_URL}{CONTRACT_DETAIL_ENDPOINT}"
    payload = request_json(session, url)
    data = payload.get("data") or []

    if isinstance(data, dict):
        contracts = list(data.values())
    elif isinstance(data, list):
        contracts = data
    else:
        raise ValueError(f"Unexpected contract detail payload: {type(data).__name__}")

    symbols: List[str] = []
    for item in contracts:
        if not isinstance(item, dict):
            continue

        symbol = item.get("symbol")
        quote = item.get("quoteCoin") or item.get("quoteCoinName")
        settle = item.get("settleCoin")
        future_type = item.get("futureType")
        state = item.get("state")

        # futureType 1 is perpetual in MEXC docs. Some responses may omit it.
        is_perp = future_type in (None, 1, "1")
        is_usdt = quote == "USDT" or settle == "USDT" or (isinstance(symbol, str) and symbol.endswith("_USDT"))
        is_active = state in (None, 0, "0")  # keep permissive; API shapes vary

        if isinstance(symbol, str) and is_perp and is_usdt and is_active:
            symbols.append(symbol)

    unique_symbols = sorted(set(symbols))
    if not unique_symbols:
        logging.warning("Symbol discovery returned no symbols; falling back to defaults")
        return DEFAULT_SYMBOLS[:top] if top and top > 0 else DEFAULT_SYMBOLS

    return unique_symbols[:top] if top and top > 0 else unique_symbols


def ensure_output_files(output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    day = utc_now().strftime("%Y%m%d")
    csv_path = output_dir / f"mexc_funding_snapshots_{day}.csv"
    jsonl_path = output_dir / f"mexc_funding_snapshots_{day}.jsonl"
    return csv_path, jsonl_path


def append_snapshots(csv_path: Path, jsonl_path: Path, snapshots: Iterable[FundingSnapshot]) -> None:
    rows = [asdict(snapshot) for snapshot in snapshots]
    if not rows:
        return

    write_header = not csv_path.exists()
    with csv_path.open("a", newline="", encoding="utf-8") as f_csv:
        writer = csv.DictWriter(f_csv, fieldnames=list(rows[0].keys()))
        if write_header:
            writer.writeheader()
        writer.writerows(rows)

    with jsonl_path.open("a", encoding="utf-8") as f_jsonl:
        for row in rows:
            f_jsonl.write(json.dumps(row, separators=(",", ":")) + "\n")


def print_snapshot_summary(snapshots: List[FundingSnapshot]) -> None:
    ok = [s for s in snapshots if s.error is None]
    failed = [s for s in snapshots if s.error is not None]
    if ok:
        top_abs = sorted(ok, key=lambda s: abs(s.funding_rate or 0), reverse=True)[:5]
        print("\nTop observed MEXC funding rates:")
        for s in top_abs:
            print(
                f"  {s.symbol:<14} rate={s.funding_rate_pct!s:>10}% "
                f"settle={s.next_settle_time_utc} mins={s.minutes_to_settle}"
            )
    if failed:
        print(f"\nFailed symbols: {len(failed)} / {len(snapshots)}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MEXC futures funding-rate logger")
    parser.add_argument(
        "--symbols",
        nargs="+",
        default=None,
        help="Specific MEXC symbols, e.g. BTC_USDT ETH_USDT SOL_USDT",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=0,
        help="Max discovered symbols to track. Use 0 for all symbols. Ignored when --symbols is supplied.",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=1800,
        help="Polling interval in seconds. Default is 1800 seconds / 30 minutes.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one snapshot and exit",
    )
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_DIR),
        help="Output directory for CSV and JSONL logs",
    )
    parser.add_argument(
        "--request-sleep",
        type=float,
        default=0.12,
        help="Sleep between symbol requests to stay comfortably inside public rate limits",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if args.interval < 10 and not args.once:
        raise ValueError("Use --interval >= 10 seconds unless running --once")

    output_dir = Path(args.output_dir)
    stopper = GracefulExit()

    with requests.Session() as session:
        session.headers.update({"User-Agent": "funding-fixedness-research/1.0"})

        if args.symbols:
            symbols = args.symbols
        else:
            symbols = discover_usdt_perp_symbols(session, top=args.top)

        logging.info("Tracking %s MEXC symbols: %s", len(symbols), ", ".join(symbols[:20]))
        if len(symbols) > 20:
            logging.info("... plus %s more", len(symbols) - 20)

        while not stopper.stop:
            csv_path, jsonl_path = ensure_output_files(output_dir)
            started = utc_now()
            snapshots: List[FundingSnapshot] = []

            for symbol in symbols:
                snapshots.append(fetch_funding_rate(session, symbol))
                time.sleep(max(args.request_sleep, 0))

            append_snapshots(csv_path, jsonl_path, snapshots)
            logging.info(
                "Wrote %s snapshots to %s and %s",
                len(snapshots),
                csv_path,
                jsonl_path,
            )
            print_snapshot_summary(snapshots)

            if args.once:
                break

            elapsed = (utc_now() - started).total_seconds()
            sleep_for = max(args.interval - elapsed, 0)
            logging.info("Sleeping %.1f seconds", sleep_for)

            end_sleep = time.time() + sleep_for
            while time.time() < end_sleep and not stopper.stop:
                time.sleep(min(1, end_sleep - time.time()))

    logging.info("Stopped MEXC funding logger")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nStopped by user")
        raise SystemExit(130)
