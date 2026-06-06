#!/usr/bin/env python3
"""
Funding settlement checker for MEXC and Bitget.

Purpose:
    Compare our live funding-rate snapshots against each exchange's historical
    settled funding-rate API.

This answers two separate questions:
    1. Fixedness: Did the displayed funding rate change before the same funding
       settlement timestamp?
    2. Settlement match: Did the final displayed pre-settlement rate match the
       historical settled funding rate recorded by the exchange?

Designed to work with the CSV files created by:
    - mexc_funding_logger.py
    - bitget_funding_logger.py

Example usage from repo root:
    python funding_settlement_checker.py --exchange both
    python funding_settlement_checker.py --exchange bitget
    python funding_settlement_checker.py --exchange mexc

Example with explicit files:
    python funding_settlement_checker.py --mexc-csv data/mexc/mexc_funding_snapshots_20260530.csv --exchange mexc
    python funding_settlement_checker.py --bitget-csv data/bitget/bitget_funding_snapshots_20260530.csv --exchange bitget

Outputs:
    data/funding_settlement_checks/funding_settlement_check_YYYYMMDD_HHMMSS.csv
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import logging
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests


MEXC_BASE_URL = "https://api.mexc.com"
MEXC_HISTORY_ENDPOINT = "/api/v1/contract/funding_rate/history"

BITGET_BASE_URL = "https://api.bitget.com"
BITGET_HISTORY_ENDPOINT = "/api/v2/mix/market/history-fund-rate"
BITGET_PRODUCT_TYPE = "USDT-FUTURES"

DEFAULT_OUTPUT_DIR = Path("data") / "funding_settlement_checks"


@dataclass
class SnapshotRow:
    exchange: str
    symbol: str
    observed_at: datetime
    settlement_time: datetime
    settlement_ms: int
    funding_rate: Decimal
    source_file: str


@dataclass
class HistoricalRate:
    exchange: str
    symbol: str
    settlement_ms: int
    settlement_time_utc: str
    funding_rate: Decimal
    raw: Dict[str, Any]


@dataclass
class SettlementCheckResult:
    exchange: str
    symbol: str
    settlement_time_utc: str
    settlement_ms: int
    observations: int
    first_observed_at_utc: str
    last_observed_before_settlement_utc: str
    first_observed_rate: str
    last_pre_settlement_rate: str
    min_observed_rate: str
    max_observed_rate: str
    distinct_observed_rates: int
    changed_before_settlement: bool
    max_observed_rate_change: str
    historical_rate: Optional[str]
    historical_settlement_time_utc: Optional[str]
    historical_match_type: str
    historical_time_diff_ms: Optional[int]
    last_vs_historical_diff: Optional[str]
    matched_historical_rate: Optional[bool]
    status: str


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_decimal(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    value_str = str(value).strip()
    if value_str == "":
        return None
    try:
        return Decimal(value_str)
    except (InvalidOperation, ValueError):
        return None


def parse_datetime_utc(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    value_str = str(value).strip()
    if not value_str:
        return None
    try:
        if value_str.endswith("Z"):
            value_str = value_str[:-1] + "+00:00"
        dt = datetime.fromisoformat(value_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def ms_to_datetime(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def datetime_to_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def decimal_to_str(value: Optional[Decimal]) -> Optional[str]:
    if value is None:
        return None
    return format(value, "f")


def abs_decimal(value: Decimal) -> Decimal:
    return value.copy_abs()


def request_json(
    session: requests.Session,
    url: str,
    params: Optional[Dict[str, Any]] = None,
    timeout: int = 10,
) -> Dict[str, Any]:
    response = session.get(url, params=params, timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError(f"Expected JSON object, got {type(payload).__name__}")
    return payload


def latest_file(pattern: str) -> Optional[Path]:
    files = [Path(p) for p in glob.glob(pattern)]
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_mtime)


def load_snapshot_csv(path: Path, exchange_hint: Optional[str] = None) -> List[SnapshotRow]:
    rows: List[SnapshotRow] = []
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for raw in reader:
            exchange = str(raw.get("exchange") or exchange_hint or "").upper()
            symbol = str(raw.get("symbol") or "").strip()
            if not exchange or not symbol:
                continue

            observed_at = parse_datetime_utc(raw.get("observed_at_utc"))

            settlement_ms: Optional[int] = None
            raw_ms = raw.get("next_settle_time_ms")
            if raw_ms not in (None, ""):
                try:
                    settlement_ms = int(float(str(raw_ms)))
                except ValueError:
                    settlement_ms = None

            settlement_time = parse_datetime_utc(raw.get("next_settle_time_utc"))
            if settlement_ms is None and settlement_time is not None:
                settlement_ms = datetime_to_ms(settlement_time)
            if settlement_time is None and settlement_ms is not None:
                settlement_time = ms_to_datetime(settlement_ms)

            funding_rate = parse_decimal(raw.get("funding_rate"))

            if observed_at is None or settlement_time is None or settlement_ms is None or funding_rate is None:
                continue

            rows.append(
                SnapshotRow(
                    exchange=exchange,
                    symbol=symbol,
                    observed_at=observed_at,
                    settlement_time=settlement_time,
                    settlement_ms=settlement_ms,
                    funding_rate=funding_rate,
                    source_file=str(path),
                )
            )
    return rows


def fetch_mexc_history(session: requests.Session, symbol: str, page_size: int = 1000) -> List[HistoricalRate]:
    url = f"{MEXC_BASE_URL}{MEXC_HISTORY_ENDPOINT}"
    payload = request_json(
        session,
        url,
        params={"symbol": symbol, "page_num": 1, "page_size": page_size},
    )
    if payload.get("success") is not True:
        raise ValueError(f"MEXC API returned non-success response for {symbol}: {payload}")

    data = payload.get("data") or {}
    result_list = data.get("resultList") or []
    if not isinstance(result_list, list):
        raise ValueError(f"Unexpected MEXC resultList for {symbol}: {type(result_list).__name__}")

    history: List[HistoricalRate] = []
    for item in result_list:
        if not isinstance(item, dict):
            continue
        settle_ms_raw = item.get("settleTime")
        rate = parse_decimal(item.get("fundingRate"))
        if settle_ms_raw is None or rate is None:
            continue
        settle_ms = int(settle_ms_raw)
        history.append(
            HistoricalRate(
                exchange="MEXC",
                symbol=str(item.get("symbol") or symbol),
                settlement_ms=settle_ms,
                settlement_time_utc=ms_to_datetime(settle_ms).isoformat(),
                funding_rate=rate,
                raw=item,
            )
        )
    return history


def fetch_bitget_history(
    session: requests.Session,
    symbol: str,
    product_type: str = BITGET_PRODUCT_TYPE,
    page_size: int = 100,
    pages: int = 3,
    request_sleep: float = 0.06,
) -> List[HistoricalRate]:
    url = f"{BITGET_BASE_URL}{BITGET_HISTORY_ENDPOINT}"
    history: List[HistoricalRate] = []

    for page_no in range(1, pages + 1):
        payload = request_json(
            session,
            url,
            params={
                "symbol": symbol,
                "productType": product_type,
                "pageSize": page_size,
                "pageNo": page_no,
            },
        )
        if payload.get("code") != "00000":
            raise ValueError(f"Bitget API returned non-success response for {symbol}: {payload}")

        data = payload.get("data") or []
        if not isinstance(data, list):
            raise ValueError(f"Unexpected Bitget data for {symbol}: {type(data).__name__}")

        if not data:
            break

        for item in data:
            if not isinstance(item, dict):
                continue
            settle_ms_raw = item.get("fundingTime")
            rate = parse_decimal(item.get("fundingRate"))
            if settle_ms_raw is None or rate is None:
                continue
            settle_ms = int(settle_ms_raw)
            history.append(
                HistoricalRate(
                    exchange="BITGET",
                    symbol=str(item.get("symbol") or symbol),
                    settlement_ms=settle_ms,
                    settlement_time_utc=ms_to_datetime(settle_ms).isoformat(),
                    funding_rate=rate,
                    raw=item,
                )
            )

        time.sleep(max(request_sleep, 0))

    return history


def match_historical_rate(
    history: List[HistoricalRate],
    settlement_ms: int,
    tolerance_ms: int,
) -> Tuple[Optional[HistoricalRate], str, Optional[int]]:
    if not history:
        return None, "no_history_returned", None

    by_exact = [h for h in history if h.settlement_ms == settlement_ms]
    if by_exact:
        return by_exact[0], "exact_ms", 0

    nearest = min(history, key=lambda h: abs(h.settlement_ms - settlement_ms))
    diff_ms = nearest.settlement_ms - settlement_ms
    if abs(diff_ms) <= tolerance_ms:
        return nearest, "nearest_within_tolerance", diff_ms

    return None, "no_timestamp_match", diff_ms


def group_snapshots(rows: Iterable[SnapshotRow]) -> Dict[Tuple[str, str, int], List[SnapshotRow]]:
    grouped: Dict[Tuple[str, str, int], List[SnapshotRow]] = {}
    for row in rows:
        key = (row.exchange, row.symbol, row.settlement_ms)
        grouped.setdefault(key, []).append(row)
    return grouped


def build_result(
    exchange: str,
    symbol: str,
    settlement_ms: int,
    observations: List[SnapshotRow],
    historical: Optional[HistoricalRate],
    historical_match_type: str,
    historical_time_diff_ms: Optional[int],
    rate_tolerance: Decimal,
) -> SettlementCheckResult:
    sorted_obs = sorted(observations, key=lambda r: r.observed_at)
    pre_settle_obs = [r for r in sorted_obs if r.observed_at <= r.settlement_time]
    comparable_obs = pre_settle_obs if pre_settle_obs else sorted_obs

    rates = [r.funding_rate for r in comparable_obs]
    first = comparable_obs[0]
    last = comparable_obs[-1]
    min_rate = min(rates)
    max_rate = max(rates)
    max_change = max_rate - min_rate
    distinct_rates = len(set(rates))
    changed_before = abs_decimal(max_change) > rate_tolerance

    hist_rate: Optional[Decimal] = historical.funding_rate if historical else None
    last_vs_hist_diff: Optional[Decimal] = None
    matched_historical: Optional[bool] = None
    if hist_rate is not None:
        last_vs_hist_diff = last.funding_rate - hist_rate
        matched_historical = abs_decimal(last_vs_hist_diff) <= rate_tolerance

    if historical is None:
        status = "missing_historical_rate"
    elif changed_before and not matched_historical:
        status = "changed_and_did_not_match_history"
    elif changed_before:
        status = "changed_but_last_matched_history"
    elif matched_historical:
        status = "fixed_and_matched_history"
    else:
        status = "fixed_but_did_not_match_history"

    return SettlementCheckResult(
        exchange=exchange,
        symbol=symbol,
        settlement_time_utc=ms_to_datetime(settlement_ms).isoformat(),
        settlement_ms=settlement_ms,
        observations=len(comparable_obs),
        first_observed_at_utc=first.observed_at.isoformat(),
        last_observed_before_settlement_utc=last.observed_at.isoformat(),
        first_observed_rate=decimal_to_str(first.funding_rate) or "",
        last_pre_settlement_rate=decimal_to_str(last.funding_rate) or "",
        min_observed_rate=decimal_to_str(min_rate) or "",
        max_observed_rate=decimal_to_str(max_rate) or "",
        distinct_observed_rates=distinct_rates,
        changed_before_settlement=changed_before,
        max_observed_rate_change=decimal_to_str(max_change) or "",
        historical_rate=decimal_to_str(hist_rate),
        historical_settlement_time_utc=historical.settlement_time_utc if historical else None,
        historical_match_type=historical_match_type,
        historical_time_diff_ms=historical_time_diff_ms,
        last_vs_historical_diff=decimal_to_str(last_vs_hist_diff),
        matched_historical_rate=matched_historical,
        status=status,
    )


def write_results(path: Path, results: List[SettlementCheckResult]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [asdict(r) for r in results]
    if not rows:
        return
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def print_summary(results: List[SettlementCheckResult]) -> None:
    if not results:
        print("No settled funding events found yet. Let the loggers run past at least one settlement time, then rerun.")
        return

    total = len(results)
    fixed_matched = sum(1 for r in results if r.status == "fixed_and_matched_history")
    changed = sum(1 for r in results if r.changed_before_settlement)
    missing = sum(1 for r in results if r.status == "missing_historical_rate")
    mismatched = sum(1 for r in results if r.matched_historical_rate is False)

    print("\nSettlement check summary")
    print(f"  Settled symbol-events checked: {total}")
    print(f"  Fixed and matched historical: {fixed_matched}")
    print(f"  Changed before settlement:    {changed}")
    print(f"  Historical missing/unmatched: {missing}")
    print(f"  Rate mismatches vs history:   {mismatched}")

    interesting = [
        r for r in results
        if r.changed_before_settlement or r.matched_historical_rate is False or r.status == "missing_historical_rate"
    ]
    if interesting:
        print("\nExamples needing review:")
        for r in interesting[:20]:
            print(
                f"  {r.exchange:<6} {r.symbol:<14} settle={r.settlement_time_utc} "
                f"status={r.status} first={r.first_observed_rate} "
                f"last={r.last_pre_settlement_rate} hist={r.historical_rate}"
            )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare live funding snapshots to historical settled funding rates")
    parser.add_argument(
        "--exchange",
        choices=["mexc", "bitget", "both"],
        default="both",
        help="Exchange snapshots to check",
    )
    parser.add_argument(
        "--mexc-csv",
        default=None,
        help="Path to MEXC snapshot CSV. Defaults to latest data/mexc/mexc_funding_snapshots_*.csv",
    )
    parser.add_argument(
        "--bitget-csv",
        default=None,
        help="Path to Bitget snapshot CSV. Defaults to latest data/bitget/bitget_funding_snapshots_*.csv",
    )
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_DIR),
        help="Output directory for settlement check CSV",
    )
    parser.add_argument(
        "--settlement-grace-minutes",
        type=int,
        default=5,
        help="Only check funding events at least this many minutes in the past",
    )
    parser.add_argument(
        "--timestamp-tolerance-ms",
        type=int,
        default=60_000,
        help="Fallback tolerance if historical settlement timestamp is not an exact millisecond match",
    )
    parser.add_argument(
        "--rate-tolerance",
        default="0.0000000001",
        help="Decimal tolerance when comparing funding rates",
    )
    parser.add_argument(
        "--bitget-product-type",
        default=BITGET_PRODUCT_TYPE,
        help="Bitget productType, default USDT-FUTURES",
    )
    parser.add_argument(
        "--bitget-history-pages",
        type=int,
        default=3,
        help="How many Bitget history pages to fetch per symbol",
    )
    parser.add_argument(
        "--request-sleep",
        type=float,
        default=0.08,
        help="Sleep between symbol-level history requests",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(asctime)s %(levelname)s %(message)s")

    rate_tolerance = parse_decimal(args.rate_tolerance)
    if rate_tolerance is None:
        raise ValueError("Invalid --rate-tolerance")

    all_rows: List[SnapshotRow] = []

    if args.exchange in ("mexc", "both"):
        mexc_path = Path(args.mexc_csv) if args.mexc_csv else latest_file("data/mexc/mexc_funding_snapshots_*.csv")
        if mexc_path and mexc_path.exists():
            mexc_rows = load_snapshot_csv(mexc_path, exchange_hint="MEXC")
            logging.info("Loaded %s MEXC snapshot rows from %s", len(mexc_rows), mexc_path)
            all_rows.extend(mexc_rows)
        else:
            logging.warning("No MEXC snapshot CSV found")

    if args.exchange in ("bitget", "both"):
        bitget_path = Path(args.bitget_csv) if args.bitget_csv else latest_file("data/bitget/bitget_funding_snapshots_*.csv")
        if bitget_path and bitget_path.exists():
            bitget_rows = load_snapshot_csv(bitget_path, exchange_hint="BITGET")
            logging.info("Loaded %s Bitget snapshot rows from %s", len(bitget_rows), bitget_path)
            all_rows.extend(bitget_rows)
        else:
            logging.warning("No Bitget snapshot CSV found")

    cutoff = utc_now() - timedelta(minutes=args.settlement_grace_minutes)
    settled_rows = [r for r in all_rows if r.settlement_time <= cutoff]
    logging.info("Rows with settlement time <= %s: %s", cutoff.isoformat(), len(settled_rows))

    grouped = group_snapshots(settled_rows)
    if not grouped:
        print_summary([])
        return 0

    results: List[SettlementCheckResult] = []
    history_cache: Dict[Tuple[str, str], List[HistoricalRate]] = {}

    with requests.Session() as session:
        session.headers.update({"User-Agent": "funding-settlement-checker/1.0"})

        for idx, ((exchange, symbol, settlement_ms), observations) in enumerate(grouped.items(), start=1):
            if idx == 1 or idx % 25 == 0 or idx == len(grouped):
                logging.info("Checking historical funding rates: %s/%s", idx, len(grouped))

            cache_key = (exchange, symbol)
            if cache_key not in history_cache:
                try:
                    if exchange == "MEXC":
                        history_cache[cache_key] = fetch_mexc_history(session, symbol)
                    elif exchange == "BITGET":
                        history_cache[cache_key] = fetch_bitget_history(
                            session,
                            symbol,
                            product_type=args.bitget_product_type,
                            pages=args.bitget_history_pages,
                            request_sleep=args.request_sleep,
                        )
                    else:
                        history_cache[cache_key] = []
                except Exception as exc:
                    logging.warning("Failed to fetch %s historical funding for %s: %s", exchange, symbol, exc)
                    history_cache[cache_key] = []

                time.sleep(max(args.request_sleep, 0))

            historical, match_type, diff_ms = match_historical_rate(
                history_cache[cache_key],
                settlement_ms=settlement_ms,
                tolerance_ms=args.timestamp_tolerance_ms,
            )

            results.append(
                build_result(
                    exchange=exchange,
                    symbol=symbol,
                    settlement_ms=settlement_ms,
                    observations=observations,
                    historical=historical,
                    historical_match_type=match_type,
                    historical_time_diff_ms=diff_ms,
                    rate_tolerance=rate_tolerance,
                )
            )

    output_dir = Path(args.output_dir)
    output_path = output_dir / f"funding_settlement_check_{utc_now().strftime('%Y%m%d_%H%M%S')}.csv"
    write_results(output_path, results)
    print_summary(results)
    print(f"\nWrote settlement check results to: {output_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nStopped by user")
        raise SystemExit(130)
