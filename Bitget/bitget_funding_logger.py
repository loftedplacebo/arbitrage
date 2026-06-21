#!/usr/bin/env python3
"""
Lightweight Bitget futures funding-rate logger.

Purpose:
    Capture repeated funding-rate snapshots so we can test whether Bitget
    funding is fixed/locked before settlement, like KuCoin, or whether it
    floats until settlement.

Default behaviour:
    - Pulls all Bitget USDT futures tickers.
    - Logs one snapshot every 30 minutes.
    - Enriches each symbol with next funding/settlement time.

Example usage:
    python bitget_funding_logger.py
    python bitget_funding_logger.py --symbols BTCUSDT ETHUSDT SOLUSDT
    python bitget_funding_logger.py --symbols BTCUSDT --once
    python bitget_funding_logger.py --top 100 --interval 1800

Notes:
    - Read-only. No API keys required.
    - Bitget public market endpoint limit is currently 20 requests/sec/IP.
    - Symbols use Bitget format, e.g. BTCUSDT, not BTC_USDT.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import requests


EXCHANGE = "BITGET"
BASE_URL = "https://api.bitget.com"
PRODUCT_TYPE = "USDT-FUTURES"
TICKERS_ENDPOINT = "/api/v2/mix/market/tickers"
FUNDING_TIME_ENDPOINT = "/api/v2/mix/market/funding-time"
DEFAULT_OUTPUT_DIR = Path("data") / "bitget"


class GracefulExit:
    """Simple stop flag; Ctrl+C is handled by KeyboardInterrupt in __main__."""

    def __init__(self) -> None:
        self.stop = False


@dataclass
class FundingSnapshot:
    exchange: str
    symbol: str
    observed_at_utc: str
    funding_rate: Optional[float]
    funding_rate_pct: Optional[float]
    funding_rate_interval_hours: Optional[int]
    next_settle_time_ms: Optional[int]
    next_settle_time_utc: Optional[str]
    minutes_to_settle: Optional[float]
    index_price: Optional[float]
    mark_price: Optional[float]
    last_price: Optional[float]
    bid_price: Optional[float]
    ask_price: Optional[float]
    base_volume_24h: Optional[float]
    quote_volume_24h: Optional[float]
    usdt_volume_24h: Optional[float]
    open_interest_amount: Optional[float]
    exchange_timestamp_ms: Optional[int]
    raw_code: Optional[str]
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
        return int(float(value))
    except (TypeError, ValueError):
        return None


def request_json(session: requests.Session, url: str, params: Optional[Dict[str, Any]] = None, timeout: int = 15) -> Dict[str, Any]:
    response = session.get(url, params=params, timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError(f"Expected JSON object, got {type(payload).__name__}")
    return payload


def assert_bitget_success(payload: Dict[str, Any]) -> None:
    code = payload.get("code")
    if code != "00000":
        raise ValueError(f"Bitget API returned non-success code={code}, msg={payload.get('msg')}")


def fetch_all_tickers(session: requests.Session) -> List[Dict[str, Any]]:
    url = f"{BASE_URL}{TICKERS_ENDPOINT}"
    payload = request_json(session, url, params={"productType": PRODUCT_TYPE})
    assert_bitget_success(payload)
    data = payload.get("data") or []
    if not isinstance(data, list):
        raise ValueError(f"Unexpected ticker data payload: {type(data).__name__}")
    return [item for item in data if isinstance(item, dict)]


def fetch_funding_time(session: requests.Session, symbol: str) -> Dict[str, Any]:
    url = f"{BASE_URL}{FUNDING_TIME_ENDPOINT}"
    payload = request_json(
        session,
        url,
        params={"symbol": symbol, "productType": PRODUCT_TYPE},
    )
    assert_bitget_success(payload)
    data = payload.get("data") or []
    if isinstance(data, list) and data:
        first = data[0]
        return first if isinstance(first, dict) else {}
    if isinstance(data, dict):
        return data
    return {}


def build_snapshot(
    ticker: Dict[str, Any],
    funding_time: Dict[str, Any],
    observed: datetime,
    raw_code: Optional[str],
) -> FundingSnapshot:
    symbol = str(ticker.get("symbol") or funding_time.get("symbol") or "")
    funding_rate = safe_float(ticker.get("fundingRate"))

    next_settle_ms = safe_int(
        funding_time.get("nextFundingTime")
        or funding_time.get("nextUpdate")
        or ticker.get("nextUpdate")
    )
    interval_hours = safe_int(
        funding_time.get("ratePeriod")
        or funding_time.get("fundingRateInterval")
        or ticker.get("fundingRateInterval")
    )

    minutes_to_settle = None
    if next_settle_ms is not None:
        minutes_to_settle = round((next_settle_ms / 1000 - observed.timestamp()) / 60, 4)

    return FundingSnapshot(
        exchange=EXCHANGE,
        symbol=symbol,
        observed_at_utc=observed.isoformat(),
        funding_rate=funding_rate,
        funding_rate_pct=round(funding_rate * 100, 8) if funding_rate is not None else None,
        funding_rate_interval_hours=interval_hours,
        next_settle_time_ms=next_settle_ms,
        next_settle_time_utc=ms_to_utc_iso(next_settle_ms),
        minutes_to_settle=minutes_to_settle,
        index_price=safe_float(ticker.get("indexPrice")),
        mark_price=safe_float(ticker.get("markPrice")),
        last_price=safe_float(ticker.get("lastPr")),
        bid_price=safe_float(ticker.get("bidPr")),
        ask_price=safe_float(ticker.get("askPr")),
        base_volume_24h=safe_float(ticker.get("baseVolume")),
        quote_volume_24h=safe_float(ticker.get("quoteVolume")),
        usdt_volume_24h=safe_float(ticker.get("usdtVolume")),
        open_interest_amount=safe_float(ticker.get("holdingAmount")),
        exchange_timestamp_ms=safe_int(ticker.get("ts")),
        raw_code=raw_code,
        error=None,
    )


def build_error_snapshot(symbol: str, observed: datetime, error: Exception) -> FundingSnapshot:
    return FundingSnapshot(
        exchange=EXCHANGE,
        symbol=symbol,
        observed_at_utc=observed.isoformat(),
        funding_rate=None,
        funding_rate_pct=None,
        funding_rate_interval_hours=None,
        next_settle_time_ms=None,
        next_settle_time_utc=None,
        minutes_to_settle=None,
        index_price=None,
        mark_price=None,
        last_price=None,
        bid_price=None,
        ask_price=None,
        base_volume_24h=None,
        quote_volume_24h=None,
        usdt_volume_24h=None,
        open_interest_amount=None,
        exchange_timestamp_ms=None,
        raw_code=None,
        error=str(error),
    )


def fetch_snapshots(session: requests.Session, symbols: Optional[List[str]], top: int, request_sleep: float) -> List[FundingSnapshot]:
    observed = utc_now()
    snapshots: List[FundingSnapshot] = []

    ticker_payload = request_json(
        session,
        f"{BASE_URL}{TICKERS_ENDPOINT}",
        params={"productType": PRODUCT_TYPE},
    )
    assert_bitget_success(ticker_payload)
    raw_code = str(ticker_payload.get("code")) if ticker_payload.get("code") is not None else None
    tickers = ticker_payload.get("data") or []
    if not isinstance(tickers, list):
        raise ValueError(f"Unexpected ticker data payload: {type(tickers).__name__}")

    ticker_by_symbol = {
        str(item.get("symbol")): item
        for item in tickers
        if isinstance(item, dict) and item.get("symbol")
    }

    if symbols:
        target_symbols = symbols
    else:
        target_symbols = sorted(ticker_by_symbol.keys())
        if top and top > 0:
            target_symbols = target_symbols[:top]

    for symbol in target_symbols:
        try:
            ticker = ticker_by_symbol.get(symbol)
            if not ticker:
                raise ValueError(f"Symbol not found in Bitget {PRODUCT_TYPE} ticker list")
            funding_time = fetch_funding_time(session, symbol)
            snapshots.append(build_snapshot(ticker, funding_time, observed, raw_code))
            time.sleep(max(request_sleep, 0))
        except Exception as exc:  # deliberately broad so logger keeps running
            logging.exception("Failed to build Bitget snapshot for %s", symbol)
            snapshots.append(build_error_snapshot(symbol, observed, exc))

    return snapshots


def ensure_output_files(output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    day = utc_now().strftime("%Y%m%d")
    csv_path = output_dir / f"bitget_funding_snapshots_{day}.csv"
    jsonl_path = output_dir / f"bitget_funding_snapshots_{day}.jsonl"
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
        top_abs = sorted(ok, key=lambda s: abs(s.funding_rate or 0), reverse=True)[:10]
        print("\nTop observed Bitget funding rates:")
        for s in top_abs:
            print(
                f"  {s.symbol:<14} rate={s.funding_rate_pct!s:>10}% "
                f"settle={s.next_settle_time_utc} mins={s.minutes_to_settle} "
                f"vol24h_usdt={s.usdt_volume_24h}"
            )

    if failed:
        print(f"\nFailed symbols: {len(failed)} / {len(snapshots)}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bitget futures funding-rate logger")
    parser.add_argument(
        "--symbols",
        nargs="+",
        default=None,
        help="Specific Bitget symbols, e.g. BTCUSDT ETHUSDT SOLUSDT",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=0,
        help="Max symbols to track from the ticker list. Use 0 for all symbols. Ignored when --symbols is supplied.",
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
        default=0.06,
        help="Sleep between per-symbol funding-time requests to stay comfortably inside public rate limits",
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

        while not stopper.stop:
            csv_path, jsonl_path = ensure_output_files(output_dir)
            started = utc_now()

            snapshots = fetch_snapshots(
                session=session,
                symbols=args.symbols,
                top=args.top,
                request_sleep=args.request_sleep,
            )

            append_snapshots(csv_path, jsonl_path, snapshots)
            logging.info(
                "Wrote %s Bitget snapshots to %s and %s",
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

    logging.info("Stopped Bitget funding logger")
    return 0


if __name__ == "__main__":
    print("BITGET MAIN STARTING")
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nStopped by user")
        raise SystemExit(130)
