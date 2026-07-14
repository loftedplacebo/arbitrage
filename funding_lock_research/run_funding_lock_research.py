#!/usr/bin/env python3
"""Capture displayed funding rates and compare them to settled funding.

The script is deliberately standalone: it does not import the scanner,
strategy, or exchange adapters. It writes only to data/funding_lock_research/
unless --output-dir is supplied.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import requests


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = REPO_ROOT / "data" / "funding_lock_research"

BINANCE_BASE_URL = "https://fapi.binance.com"
MEXC_BASE_URL = os.environ.get("MEXC_CONTRACT_BASE_URL", "https://api.mexc.com")
OKX_BASE_URL = "https://www.okx.com"

BUCKETS_MINUTES = [1, 5, 15, 30, 60, 120, 240, 480]
SNAPSHOT_FIELDS = [
    "exchange",
    "symbol",
    "observed_at_utc",
    "current_funding_rate",
    "predicted_funding_rate",
    "next_funding_rate",
    "settlement_time_ms",
    "settlement_time_utc",
    "next_settlement_time_ms",
    "next_settlement_time_utc",
    "minutes_to_settlement",
    "funding_interval_hours",
    "index_price",
    "mark_price",
    "mark_index_basis_pct",
    "last_price",
    "source_endpoint",
]
COMPARISON_FIELDS = [
    "exchange",
    "symbol",
    "settlement_time_ms",
    "settlement_time_utc",
    "rate_field",
    "observed_at_utc",
    "minutes_to_settlement",
    "bucket",
    "estimated_rate",
    "settled_rate",
    "signed_error",
    "abs_error",
    "matched_within_tolerance",
    "sign_flipped",
    "positive_receiver_reversed",
    "negative_receiver_reversed",
    "history_match_type",
    "history_time_diff_ms",
]
SCORE_FIELDS = [
    "exchange",
    "symbol",
    "rate_field",
    "bucket",
    "events",
    "observations",
    "matched_pct",
    "sign_flip_pct",
    "positive_receiver_reversal_pct",
    "negative_receiver_reversal_pct",
    "mean_abs_error",
    "p95_abs_error",
    "max_abs_error",
]
EVENT_FIELDS = [
    "exchange",
    "symbol",
    "settlement_time_ms",
    "settlement_time_utc",
    "rate_field",
    "observations",
    "first_observed_at_utc",
    "last_observed_at_utc",
    "first_estimated_rate",
    "last_estimated_rate",
    "settled_rate",
    "distinct_estimated_rates",
    "changed_before_settlement",
    "last_matched_settled",
    "status",
]
BASIS_ADJUSTED_FIELDS = [
    "exchange",
    "symbol",
    "settlement_time_ms",
    "settlement_time_utc",
    "first_seen_bucket",
    "first_seen_minutes_to_settlement",
    "last_seen_minutes_to_settlement",
    "max_abs_estimated_rate_pct",
    "first_estimated_rate_pct",
    "settled_rate_pct",
    "funding_benefit_pct",
    "entry_basis_pct",
    "exit_basis_pct",
    "basis_pnl_pct",
    "estimated_net_pct",
    "same_funding_direction",
    "basis_available",
]


@dataclass(frozen=True)
class FundingSnapshot:
    exchange: str
    symbol: str
    observed_at_utc: str
    current_funding_rate: Optional[str]
    predicted_funding_rate: Optional[str]
    next_funding_rate: Optional[str]
    settlement_time_ms: Optional[int]
    settlement_time_utc: Optional[str]
    next_settlement_time_ms: Optional[int]
    next_settlement_time_utc: Optional[str]
    minutes_to_settlement: Optional[float]
    funding_interval_hours: Optional[float]
    index_price: Optional[str]
    mark_price: Optional[str]
    mark_index_basis_pct: Optional[float]
    last_price: Optional[str]
    source_endpoint: str


@dataclass(frozen=True)
class HistoricalFunding:
    exchange: str
    symbol: str
    settlement_time_ms: int
    settlement_time_utc: str
    funding_rate: Decimal


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def ms_to_datetime(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def ms_to_iso(ms: Optional[int]) -> Optional[str]:
    return ms_to_datetime(ms).isoformat() if ms is not None else None


def datetime_to_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def parse_decimal(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def decimal_to_str(value: Optional[Decimal]) -> Optional[str]:
    if value is None:
        return None
    return format(value, "f")


def parse_int_ms(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return None


def parse_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def minutes_until_ms(target_ms: Optional[int], now: datetime) -> Optional[float]:
    if target_ms is None:
        return None
    return (ms_to_datetime(target_ms) - now).total_seconds() / 60


def bucket_for_minutes(minutes: Optional[float]) -> str:
    if minutes is None:
        return "unknown"
    if minutes < 0:
        return "after_settlement"
    for bucket in BUCKETS_MINUTES:
        if minutes <= bucket:
            return f"lte_{bucket}m"
    return f"gt_{BUCKETS_MINUTES[-1]}m"


def first_seen_bucket(minutes: Optional[float]) -> str:
    if minutes is None:
        return "unknown"
    if minutes <= 15:
        return "first_seen_lte_15m"
    if minutes <= 30:
        return "first_seen_lte_30m"
    if minutes <= 60:
        return "first_seen_lte_60m"
    if minutes <= 120:
        return "first_seen_lte_120m"
    if minutes <= 240:
        return "first_seen_lte_240m"
    if minutes <= 480:
        return "first_seen_lte_480m"
    return "first_seen_gt_480m"


def basis_pct(index_price: Any, mark_price: Any) -> Optional[float]:
    index_decimal = parse_decimal(index_price)
    mark_decimal = parse_decimal(mark_price)
    if index_decimal is None or mark_decimal is None or index_decimal == 0:
        return None
    return float((mark_decimal - index_decimal) / index_decimal * Decimal("100"))


def percentile(values: Sequence[Decimal], pct: float) -> Optional[Decimal]:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * pct
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[int(position)]
    fraction = Decimal(str(position - lower))
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


class PublicHttp:
    def __init__(self, request_sleep: float, timeout: int = 15) -> None:
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "funding-lock-research/1.0"})
        self.request_sleep = max(request_sleep, 0.0)
        self.timeout = timeout

    def get_json(self, base_url: str, path: str, params: Optional[dict] = None) -> Any:
        url = f"{base_url}{path}"
        response = self.session.get(url, params=params, timeout=self.timeout)
        response.raise_for_status()
        if self.request_sleep:
            time.sleep(self.request_sleep)
        return response.json()


class ExchangeClient:
    exchange: str

    def discover_symbols(self, limit: Optional[int]) -> List[str]:
        raise NotImplementedError

    def fetch_snapshots(self, symbols: Optional[List[str]], limit: Optional[int]) -> List[FundingSnapshot]:
        raise NotImplementedError

    def fetch_history(self, symbol: str) -> List[HistoricalFunding]:
        raise NotImplementedError


class BinanceFundingClient(ExchangeClient):
    exchange = "BINANCE"

    def __init__(self, http: PublicHttp) -> None:
        self.http = http

    def discover_symbols(self, limit: Optional[int]) -> List[str]:
        payload = self.http.get_json(BINANCE_BASE_URL, "/fapi/v1/exchangeInfo")
        symbols = []
        for item in payload.get("symbols", []):
            if (
                item.get("contractType") == "PERPETUAL"
                and item.get("quoteAsset") == "USDT"
                and item.get("status") == "TRADING"
            ):
                symbols.append(str(item.get("symbol")))
        symbols = sorted(set(symbols))
        return symbols[:limit] if limit else symbols

    def fetch_snapshots(self, symbols: Optional[List[str]], limit: Optional[int]) -> List[FundingSnapshot]:
        now = utc_now()
        selected = set(symbols or [])
        payload = self.http.get_json(BINANCE_BASE_URL, "/fapi/v1/premiumIndex")
        rows = payload if isinstance(payload, list) else [payload]
        snapshots: List[FundingSnapshot] = []
        for item in rows:
            if not isinstance(item, dict):
                continue
            symbol = str(item.get("symbol") or "")
            if not symbol or (selected and symbol not in selected):
                continue
            settlement_ms = parse_int_ms(item.get("nextFundingTime"))
            snapshots.append(
                FundingSnapshot(
                    exchange=self.exchange,
                    symbol=symbol,
                    observed_at_utc=now.isoformat(),
                    current_funding_rate=decimal_to_str(parse_decimal(item.get("lastFundingRate"))),
                    predicted_funding_rate=None,
                    next_funding_rate=None,
                    settlement_time_ms=settlement_ms,
                    settlement_time_utc=ms_to_iso(settlement_ms),
                    next_settlement_time_ms=None,
                    next_settlement_time_utc=None,
                    minutes_to_settlement=minutes_until_ms(settlement_ms, now),
                    funding_interval_hours=8.0,
                    index_price=str(item.get("indexPrice") or "") or None,
                    mark_price=str(item.get("markPrice") or "") or None,
                    mark_index_basis_pct=basis_pct(item.get("indexPrice"), item.get("markPrice")),
                    last_price=None,
                    source_endpoint="/fapi/v1/premiumIndex",
                )
            )
            if limit and len(snapshots) >= limit:
                break
        return snapshots

    def fetch_history(self, symbol: str) -> List[HistoricalFunding]:
        payload = self.http.get_json(
            BINANCE_BASE_URL,
            "/fapi/v1/fundingRate",
            params={"symbol": symbol, "limit": 1000},
        )
        history = []
        for item in payload if isinstance(payload, list) else []:
            settle_ms = parse_int_ms(item.get("fundingTime"))
            rate = parse_decimal(item.get("fundingRate"))
            if settle_ms is None or rate is None:
                continue
            history.append(
                HistoricalFunding(
                    exchange=self.exchange,
                    symbol=symbol,
                    settlement_time_ms=settle_ms,
                    settlement_time_utc=ms_to_iso(settle_ms) or "",
                    funding_rate=rate,
                )
            )
        return history


class MexcFundingClient(ExchangeClient):
    exchange = "MEXC"

    def __init__(self, http: PublicHttp) -> None:
        self.http = http

    def discover_symbols(self, limit: Optional[int]) -> List[str]:
        payload = self.http.get_json(MEXC_BASE_URL, "/api/v1/contract/detail")
        data = payload.get("data") if isinstance(payload, dict) else []
        if isinstance(data, dict):
            data = list(data.values())
        symbols = []
        for item in data if isinstance(data, list) else []:
            if not isinstance(item, dict):
                continue
            state = item.get("state")
            if (
                item.get("quoteCoin") == "USDT"
                and item.get("settleCoin") == "USDT"
                and state in (None, 0, "0")
            ):
                symbol = str(item.get("symbol") or "")
                if symbol:
                    symbols.append(symbol)
        symbols = sorted(set(symbols))
        return symbols[:limit] if limit else symbols

    def fetch_snapshots(self, symbols: Optional[List[str]], limit: Optional[int]) -> List[FundingSnapshot]:
        now = utc_now()
        selected = symbols or self.discover_symbols(limit)
        if limit:
            selected = selected[:limit]
        snapshots: List[FundingSnapshot] = []
        for idx, symbol in enumerate(selected, start=1):
            payload = self.http.get_json(MEXC_BASE_URL, f"/api/v1/contract/funding_rate/{symbol}")
            data = payload.get("data") if isinstance(payload, dict) else {}
            if not isinstance(data, dict):
                data = {}
            settlement_ms = parse_int_ms(data.get("nextSettleTime"))
            index_price = data.get("indexPrice") or data.get("idxPrice")
            mark_price = data.get("fairPrice")
            snapshots.append(
                FundingSnapshot(
                    exchange=self.exchange,
                    symbol=symbol,
                    observed_at_utc=now.isoformat(),
                    current_funding_rate=decimal_to_str(parse_decimal(data.get("fundingRate"))),
                    predicted_funding_rate=None,
                    next_funding_rate=None,
                    settlement_time_ms=settlement_ms,
                    settlement_time_utc=ms_to_iso(settlement_ms),
                    next_settlement_time_ms=None,
                    next_settlement_time_utc=None,
                    minutes_to_settlement=minutes_until_ms(settlement_ms, now),
                    funding_interval_hours=parse_float(data.get("collectCycle")),
                    index_price=str(index_price or "") or None,
                    mark_price=str(mark_price or "") or None,
                    mark_index_basis_pct=basis_pct(index_price, mark_price),
                    last_price=None,
                    source_endpoint="/api/v1/contract/funding_rate/{symbol}",
                )
            )
            if idx % 100 == 0:
                print(f"MEXC: captured {idx}/{len(selected)} symbols", flush=True)
        return snapshots

    def fetch_history(self, symbol: str) -> List[HistoricalFunding]:
        payload = self.http.get_json(
            MEXC_BASE_URL,
            "/api/v1/contract/funding_rate/history",
            params={"symbol": symbol, "page_num": 1, "page_size": 1000},
        )
        data = payload.get("data") if isinstance(payload, dict) else {}
        result_list = data.get("resultList") if isinstance(data, dict) else []
        history = []
        for item in result_list if isinstance(result_list, list) else []:
            settle_ms = parse_int_ms(item.get("settleTime"))
            rate = parse_decimal(item.get("fundingRate"))
            if settle_ms is None or rate is None:
                continue
            history.append(
                HistoricalFunding(
                    exchange=self.exchange,
                    symbol=symbol,
                    settlement_time_ms=settle_ms,
                    settlement_time_utc=ms_to_iso(settle_ms) or "",
                    funding_rate=rate,
                )
            )
        return history


class OkxFundingClient(ExchangeClient):
    exchange = "OKX"

    def __init__(self, http: PublicHttp) -> None:
        self.http = http

    def discover_symbols(self, limit: Optional[int]) -> List[str]:
        payload = self.http.get_json(OKX_BASE_URL, "/api/v5/public/instruments", params={"instType": "SWAP"})
        data = payload.get("data") if isinstance(payload, dict) else []
        symbols = []
        for item in data if isinstance(data, list) else []:
            if not isinstance(item, dict):
                continue
            if item.get("settleCcy") == "USDT" and item.get("state") == "live":
                symbol = str(item.get("instId") or "")
                if symbol:
                    symbols.append(symbol)
        symbols = sorted(set(symbols))
        return symbols[:limit] if limit else symbols

    def fetch_mark_prices(self) -> Dict[str, str]:
        payload = self.http.get_json(OKX_BASE_URL, "/api/v5/public/mark-price", params={"instType": "SWAP"})
        data = payload.get("data") if isinstance(payload, dict) else []
        prices: Dict[str, str] = {}
        for item in data if isinstance(data, list) else []:
            if not isinstance(item, dict):
                continue
            inst_id = str(item.get("instId") or "")
            mark_px = item.get("markPx")
            if inst_id and mark_px not in (None, ""):
                prices[inst_id] = str(mark_px)
        return prices

    def fetch_index_prices(self) -> Dict[str, str]:
        payload = self.http.get_json(OKX_BASE_URL, "/api/v5/market/index-tickers", params={"quoteCcy": "USDT"})
        data = payload.get("data") if isinstance(payload, dict) else []
        prices: Dict[str, str] = {}
        for item in data if isinstance(data, list) else []:
            if not isinstance(item, dict):
                continue
            inst_id = str(item.get("instId") or "")
            idx_px = item.get("idxPx")
            if inst_id and idx_px not in (None, ""):
                prices[inst_id] = str(idx_px)
        return prices

    @staticmethod
    def index_id_for_swap(symbol: str) -> str:
        if symbol.endswith("-SWAP"):
            return symbol[: -len("-SWAP")]
        return symbol

    def fetch_snapshots(self, symbols: Optional[List[str]], limit: Optional[int]) -> List[FundingSnapshot]:
        now = utc_now()
        selected = symbols or self.discover_symbols(limit)
        if limit:
            selected = selected[:limit]
        mark_prices = self.fetch_mark_prices()
        index_prices = self.fetch_index_prices()
        snapshots: List[FundingSnapshot] = []
        for idx, symbol in enumerate(selected, start=1):
            payload = self.http.get_json(OKX_BASE_URL, "/api/v5/public/funding-rate", params={"instId": symbol})
            data = payload.get("data") if isinstance(payload, dict) else []
            item = data[0] if isinstance(data, list) and data else {}
            if not isinstance(item, dict):
                item = {}
            settlement_ms = parse_int_ms(item.get("fundingTime"))
            next_settlement_ms = parse_int_ms(item.get("nextFundingTime"))
            mark_price = mark_prices.get(symbol)
            index_price = index_prices.get(self.index_id_for_swap(symbol))
            snapshots.append(
                FundingSnapshot(
                    exchange=self.exchange,
                    symbol=symbol,
                    observed_at_utc=now.isoformat(),
                    current_funding_rate=decimal_to_str(parse_decimal(item.get("fundingRate"))),
                    predicted_funding_rate=None,
                    next_funding_rate=decimal_to_str(parse_decimal(item.get("nextFundingRate"))),
                    settlement_time_ms=settlement_ms,
                    settlement_time_utc=ms_to_iso(settlement_ms),
                    next_settlement_time_ms=next_settlement_ms,
                    next_settlement_time_utc=ms_to_iso(next_settlement_ms),
                    minutes_to_settlement=minutes_until_ms(settlement_ms, now),
                    funding_interval_hours=None,
                    index_price=index_price,
                    mark_price=mark_price,
                    mark_index_basis_pct=basis_pct(index_price, mark_price),
                    last_price=None,
                    source_endpoint="/api/v5/public/funding-rate",
                )
            )
            if idx % 100 == 0:
                print(f"OKX: captured {idx}/{len(selected)} symbols", flush=True)
        return snapshots

    def fetch_history(self, symbol: str) -> List[HistoricalFunding]:
        payload = self.http.get_json(
            OKX_BASE_URL,
            "/api/v5/public/funding-rate-history",
            params={"instId": symbol, "limit": 100},
        )
        data = payload.get("data") if isinstance(payload, dict) else []
        history = []
        for item in data if isinstance(data, list) else []:
            settle_ms = parse_int_ms(item.get("fundingTime"))
            rate = parse_decimal(item.get("fundingRate"))
            if settle_ms is None or rate is None:
                continue
            history.append(
                HistoricalFunding(
                    exchange=self.exchange,
                    symbol=symbol,
                    settlement_time_ms=settle_ms,
                    settlement_time_utc=ms_to_iso(settle_ms) or "",
                    funding_rate=rate,
                )
            )
        return history


def build_clients(http: PublicHttp) -> Dict[str, ExchangeClient]:
    return {
        "BINANCE": BinanceFundingClient(http),
        "MEXC": MexcFundingClient(http),
        "OKX": OkxFundingClient(http),
    }


def append_csv(path: Path, rows: Iterable[dict], fieldnames: Sequence[str]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = list(rows)
    if not rows:
        return 0
    exists = path.exists() and path.stat().st_size > 0
    with path.open("a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        if not exists:
            writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field) for field in fieldnames})
    return len(rows)


def write_csv(path: Path, rows: Iterable[dict], fieldnames: Sequence[str]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = list(rows)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field) for field in fieldnames})
    tmp_path.replace(path)
    return len(rows)


def snapshot_path(output_dir: Path, now: datetime) -> Path:
    return output_dir / "snapshots" / f"funding_rate_snapshots_{now.strftime('%Y%m%d')}.csv"


def load_snapshots(output_dir: Path) -> List[dict]:
    rows: List[dict] = []
    for path in sorted((output_dir / "snapshots").glob("funding_rate_snapshots_*.csv")):
        with path.open("r", encoding="utf-8", newline="") as f:
            rows.extend(csv.DictReader(f))
    return rows


def estimate_rows_from_snapshot(row: dict) -> List[dict]:
    estimates = []
    for field, time_key in (
        ("current_funding_rate", "settlement_time_ms"),
        ("predicted_funding_rate", "next_settlement_time_ms"),
        ("next_funding_rate", "next_settlement_time_ms"),
    ):
        rate = parse_decimal(row.get(field))
        target_ms = parse_int_ms(row.get(time_key))
        if field in ("predicted_funding_rate", "next_funding_rate") and target_ms is None:
            target_ms = parse_int_ms(row.get("settlement_time_ms"))
        if rate is None or target_ms is None:
            continue
        observed_at = row.get("observed_at_utc") or ""
        try:
            observed_dt = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
            if observed_dt.tzinfo is None:
                observed_dt = observed_dt.replace(tzinfo=timezone.utc)
            minutes_to_settlement = (ms_to_datetime(target_ms) - observed_dt).total_seconds() / 60
        except ValueError:
            minutes_to_settlement = None
        estimates.append(
            {
                "exchange": row.get("exchange"),
                "symbol": row.get("symbol"),
                "settlement_time_ms": target_ms,
                "settlement_time_utc": ms_to_iso(target_ms),
                "rate_field": field,
                "observed_at_utc": observed_at,
                "minutes_to_settlement": minutes_to_settlement,
                "bucket": bucket_for_minutes(minutes_to_settlement),
                "estimated_rate": rate,
            }
        )
    return estimates


def match_history(
    history: Sequence[HistoricalFunding],
    settlement_ms: int,
    tolerance_ms: int,
) -> Tuple[Optional[HistoricalFunding], str, Optional[int]]:
    exact = [item for item in history if item.settlement_time_ms == settlement_ms]
    if exact:
        return exact[0], "exact_ms", 0
    if not history:
        return None, "no_history", None
    nearest = min(history, key=lambda item: abs(item.settlement_time_ms - settlement_ms))
    diff = nearest.settlement_time_ms - settlement_ms
    if abs(diff) <= tolerance_ms:
        return nearest, "nearest_within_tolerance", diff
    return None, "no_timestamp_match", diff


def reconcile(
    output_dir: Path,
    clients: Dict[str, ExchangeClient],
    exchanges: Sequence[str],
    grace_minutes: float,
    history_tolerance_minutes: float,
    rate_tolerance: Decimal,
    min_abs_basis_funding_rate: Decimal,
) -> Tuple[int, int, int]:
    now = utc_now()
    cutoff_ms = datetime_to_ms(now - timedelta(minutes=grace_minutes))
    history_tolerance_ms = int(history_tolerance_minutes * 60 * 1000)
    snapshot_rows = load_snapshots(output_dir)
    estimates = [
        estimate
        for row in snapshot_rows
        for estimate in estimate_rows_from_snapshot(row)
        if estimate["exchange"] in exchanges
        and int(estimate["settlement_time_ms"]) <= cutoff_ms
    ]

    history_cache: Dict[Tuple[str, str], List[HistoricalFunding]] = {}
    comparisons = []
    for estimate in estimates:
        key = (str(estimate["exchange"]), str(estimate["symbol"]))
        client = clients.get(key[0])
        if client is None:
            continue
        if key not in history_cache:
            try:
                history_cache[key] = client.fetch_history(key[1])
            except Exception as exc:
                print(f"History fetch failed for {key[0]} {key[1]}: {type(exc).__name__}: {exc}", flush=True)
                history_cache[key] = []
        historical, match_type, diff_ms = match_history(
            history_cache[key],
            int(estimate["settlement_time_ms"]),
            history_tolerance_ms,
        )
        if historical is None:
            continue
        estimated_rate: Decimal = estimate["estimated_rate"]
        signed_error = estimated_rate - historical.funding_rate
        abs_error = signed_error.copy_abs()
        sign_flipped = (
            estimated_rate != 0
            and historical.funding_rate != 0
            and ((estimated_rate > 0 and historical.funding_rate < 0) or (estimated_rate < 0 and historical.funding_rate > 0))
        )
        comparisons.append(
            {
                "exchange": estimate["exchange"],
                "symbol": estimate["symbol"],
                "settlement_time_ms": estimate["settlement_time_ms"],
                "settlement_time_utc": estimate["settlement_time_utc"],
                "rate_field": estimate["rate_field"],
                "observed_at_utc": estimate["observed_at_utc"],
                "minutes_to_settlement": estimate["minutes_to_settlement"],
                "bucket": estimate["bucket"],
                "estimated_rate": decimal_to_str(estimated_rate),
                "settled_rate": decimal_to_str(historical.funding_rate),
                "signed_error": decimal_to_str(signed_error),
                "abs_error": decimal_to_str(abs_error),
                "matched_within_tolerance": abs_error <= rate_tolerance,
                "sign_flipped": sign_flipped,
                "positive_receiver_reversed": estimated_rate > 0 and historical.funding_rate <= 0,
                "negative_receiver_reversed": estimated_rate < 0 and historical.funding_rate >= 0,
                "history_match_type": match_type,
                "history_time_diff_ms": diff_ms,
            }
        )

    comparison_count = write_csv(output_dir / "comparisons" / "funding_rate_comparisons_all.csv", comparisons, COMPARISON_FIELDS)
    event_count = write_csv(output_dir / "reports" / "funding_lock_events.csv", build_event_rows(comparisons, rate_tolerance), EVENT_FIELDS)
    score_count = write_csv(output_dir / "reports" / "funding_lock_scores.csv", build_score_rows(comparisons), SCORE_FIELDS)
    basis_count = write_csv(
        output_dir / "reports" / "basis_adjusted_events.csv",
        build_basis_adjusted_event_rows(
            snapshot_rows=snapshot_rows,
            comparisons=comparisons,
            min_abs_funding_rate=Decimal(str(min_abs_basis_funding_rate)),
        ),
        BASIS_ADJUSTED_FIELDS,
    )
    if basis_count:
        print(f"Wrote {basis_count} basis-adjusted funding events", flush=True)
    return comparison_count, event_count, score_count


def build_event_rows(comparisons: Sequence[dict], rate_tolerance: Decimal) -> List[dict]:
    grouped: Dict[Tuple[str, str, str, str], List[dict]] = {}
    for row in comparisons:
        key = (
            str(row["exchange"]),
            str(row["symbol"]),
            str(row["settlement_time_ms"]),
            str(row["rate_field"]),
        )
        grouped.setdefault(key, []).append(row)

    event_rows = []
    for (exchange, symbol, settlement_ms, rate_field), rows in sorted(grouped.items()):
        ordered = sorted(rows, key=lambda item: item["observed_at_utc"])
        estimates = [parse_decimal(item["estimated_rate"]) for item in ordered]
        estimates = [item for item in estimates if item is not None]
        settled = parse_decimal(ordered[-1].get("settled_rate"))
        distinct = len(set(estimates))
        changed = distinct > 1
        last_estimate = estimates[-1] if estimates else None
        last_matched = (
            settled is not None
            and last_estimate is not None
            and (last_estimate - settled).copy_abs() <= rate_tolerance
        )
        if changed and not last_matched:
            status = "changed_and_last_did_not_match"
        elif changed:
            status = "changed_but_last_matched"
        elif last_matched:
            status = "fixed_and_matched"
        else:
            status = "fixed_but_last_did_not_match"
        event_rows.append(
            {
                "exchange": exchange,
                "symbol": symbol,
                "settlement_time_ms": settlement_ms,
                "settlement_time_utc": ordered[-1].get("settlement_time_utc"),
                "rate_field": rate_field,
                "observations": len(ordered),
                "first_observed_at_utc": ordered[0].get("observed_at_utc"),
                "last_observed_at_utc": ordered[-1].get("observed_at_utc"),
                "first_estimated_rate": decimal_to_str(estimates[0]) if estimates else None,
                "last_estimated_rate": decimal_to_str(last_estimate),
                "settled_rate": decimal_to_str(settled),
                "distinct_estimated_rates": distinct,
                "changed_before_settlement": changed,
                "last_matched_settled": last_matched,
                "status": status,
            }
        )
    return event_rows


def build_score_rows(comparisons: Sequence[dict]) -> List[dict]:
    grouped: Dict[Tuple[str, str, str, str], List[dict]] = {}
    event_keys: Dict[Tuple[str, str, str, str], set] = {}
    for row in comparisons:
        key = (
            str(row["exchange"]),
            str(row["symbol"]),
            str(row["rate_field"]),
            str(row["bucket"]),
        )
        grouped.setdefault(key, []).append(row)
        event_keys.setdefault(key, set()).add(str(row["settlement_time_ms"]))

    score_rows = []
    for (exchange, symbol, rate_field, bucket), rows in sorted(grouped.items()):
        abs_errors = [parse_decimal(row.get("abs_error")) for row in rows]
        abs_errors = [item for item in abs_errors if item is not None]
        observations = len(rows)
        matched = sum(1 for row in rows if str(row.get("matched_within_tolerance")).lower() == "true")
        sign_flips = sum(1 for row in rows if str(row.get("sign_flipped")).lower() == "true")
        pos_reversals = sum(1 for row in rows if str(row.get("positive_receiver_reversed")).lower() == "true")
        neg_reversals = sum(1 for row in rows if str(row.get("negative_receiver_reversed")).lower() == "true")
        mean_abs = sum(abs_errors, Decimal("0")) / Decimal(len(abs_errors)) if abs_errors else None
        score_rows.append(
            {
                "exchange": exchange,
                "symbol": symbol,
                "rate_field": rate_field,
                "bucket": bucket,
                "events": len(event_keys.get((exchange, symbol, rate_field, bucket), set())),
                "observations": observations,
                "matched_pct": matched / observations * 100 if observations else None,
                "sign_flip_pct": sign_flips / observations * 100 if observations else None,
                "positive_receiver_reversal_pct": pos_reversals / observations * 100 if observations else None,
                "negative_receiver_reversal_pct": neg_reversals / observations * 100 if observations else None,
                "mean_abs_error": decimal_to_str(mean_abs),
                "p95_abs_error": decimal_to_str(percentile(abs_errors, 0.95)),
                "max_abs_error": decimal_to_str(max(abs_errors) if abs_errors else None),
            }
        )
    return score_rows


def build_basis_adjusted_event_rows(
    snapshot_rows: Sequence[dict],
    comparisons: Sequence[dict],
    min_abs_funding_rate: Decimal,
) -> List[dict]:
    settled_rates: Dict[Tuple[str, str, str], Decimal] = {}
    settlement_iso: Dict[Tuple[str, str, str], str] = {}
    for row in comparisons:
        if row.get("rate_field") != "current_funding_rate":
            continue
        key = (
            str(row.get("exchange") or ""),
            str(row.get("symbol") or ""),
            str(row.get("settlement_time_ms") or ""),
        )
        settled = parse_decimal(row.get("settled_rate"))
        if settled is None or not all(key):
            continue
        settled_rates[key] = settled
        settlement_iso[key] = str(row.get("settlement_time_utc") or "")

    grouped: Dict[Tuple[str, str, str], List[dict]] = {}
    for row in snapshot_rows:
        if row.get("exchange") not in ("BINANCE", "MEXC"):
            continue
        key = (
            str(row.get("exchange") or ""),
            str(row.get("symbol") or ""),
            str(row.get("settlement_time_ms") or ""),
        )
        if key not in settled_rates:
            continue
        rate = parse_decimal(row.get("current_funding_rate"))
        if rate is None:
            continue
        minutes = parse_float(row.get("minutes_to_settlement"))
        basis = parse_float(row.get("mark_index_basis_pct"))
        if basis is None:
            basis = basis_pct(row.get("index_price"), row.get("mark_price"))
        grouped.setdefault(key, []).append(
            {
                "observed_at_utc": row.get("observed_at_utc"),
                "minutes_to_settlement": minutes,
                "estimated_rate": rate,
                "basis_pct": basis,
            }
        )

    report_rows: List[dict] = []
    for (exchange, symbol, settlement_ms), rows in sorted(grouped.items()):
        threshold_rows = [
            row
            for row in rows
            if abs(row["estimated_rate"]) >= min_abs_funding_rate
            and row.get("basis_pct") is not None
        ]
        if not threshold_rows:
            continue

        first = max(
            threshold_rows,
            key=lambda row: -1 if row.get("minutes_to_settlement") is None else row["minutes_to_settlement"],
        )
        pre_settle_rows = [
            row for row in rows
            if row.get("minutes_to_settlement") is not None
            and row["minutes_to_settlement"] >= 0
            and row.get("basis_pct") is not None
        ]
        last = min(
            pre_settle_rows or threshold_rows,
            key=lambda row: 10**9 if row.get("minutes_to_settlement") is None else row["minutes_to_settlement"],
        )

        direction = Decimal("1") if first["estimated_rate"] > 0 else Decimal("-1")
        settled = settled_rates[(exchange, symbol, settlement_ms)]
        funding_benefit_pct = float(direction * settled * Decimal("100"))
        entry_basis = float(first["basis_pct"])
        exit_basis = float(last["basis_pct"])
        basis_pnl_pct = float(direction) * (entry_basis - exit_basis)
        estimated_net_pct = funding_benefit_pct + basis_pnl_pct
        max_abs_estimated_rate_pct = max(abs(row["estimated_rate"]) for row in threshold_rows) * Decimal("100")

        report_rows.append(
            {
                "exchange": exchange,
                "symbol": symbol,
                "settlement_time_ms": settlement_ms,
                "settlement_time_utc": settlement_iso.get((exchange, symbol, settlement_ms), ""),
                "first_seen_bucket": first_seen_bucket(first.get("minutes_to_settlement")),
                "first_seen_minutes_to_settlement": first.get("minutes_to_settlement"),
                "last_seen_minutes_to_settlement": last.get("minutes_to_settlement"),
                "max_abs_estimated_rate_pct": decimal_to_str(max_abs_estimated_rate_pct),
                "first_estimated_rate_pct": decimal_to_str(first["estimated_rate"] * Decimal("100")),
                "settled_rate_pct": decimal_to_str(settled * Decimal("100")),
                "funding_benefit_pct": funding_benefit_pct,
                "entry_basis_pct": entry_basis,
                "exit_basis_pct": exit_basis,
                "basis_pnl_pct": basis_pnl_pct,
                "estimated_net_pct": estimated_net_pct,
                "same_funding_direction": funding_benefit_pct > 0,
                "basis_available": True,
            }
        )
    return report_rows


def parse_exchange_list(value: str) -> List[str]:
    exchanges = [item.strip().upper() for item in value.split(",") if item.strip()]
    unknown = [item for item in exchanges if item not in {"BINANCE", "MEXC", "OKX"}]
    if unknown:
        raise SystemExit(f"Unsupported exchange(s): {', '.join(unknown)}")
    return exchanges


def parse_symbols(value: Optional[str]) -> Optional[List[str]]:
    if not value:
        return None
    return [item.strip() for item in value.split(",") if item.strip()]


def capture_once(
    output_dir: Path,
    clients: Dict[str, ExchangeClient],
    exchanges: Sequence[str],
    symbols: Optional[List[str]],
    max_symbols: Optional[int],
) -> int:
    total_captured = 0
    for exchange in exchanges:
        client = clients[exchange]
        try:
            snapshots = client.fetch_snapshots(symbols, max_symbols)
        except Exception as exc:
            print(f"Snapshot fetch failed for {exchange}: {type(exc).__name__}: {exc}", flush=True)
            continue
        rows = [asdict(snapshot) for snapshot in snapshots]
        written = append_csv(snapshot_path(output_dir, utc_now()), rows, SNAPSHOT_FIELDS)
        total_captured += written
        print(f"{exchange}: captured and wrote {written} funding snapshots", flush=True)

    return total_captured


def run_once(args: argparse.Namespace) -> None:
    output_dir = Path(args.output_dir)
    http = PublicHttp(request_sleep=args.request_sleep, timeout=args.timeout)
    clients = build_clients(http)
    exchanges = parse_exchange_list(args.exchanges)
    symbols = parse_symbols(args.symbols)
    max_symbols = args.max_symbols if args.max_symbols and args.max_symbols > 0 else None

    captured = capture_once(output_dir, clients, exchanges, symbols, max_symbols)
    print(f"Wrote {captured} snapshots under {output_dir}", flush=True)

    if not args.no_reconcile:
        comparisons, events, scores = reconcile(
            output_dir=output_dir,
            clients=clients,
            exchanges=exchanges,
            grace_minutes=args.settlement_grace_minutes,
            history_tolerance_minutes=args.history_tolerance_minutes,
            rate_tolerance=Decimal(str(args.rate_tolerance)),
            min_abs_basis_funding_rate=Decimal(str(args.min_abs_basis_funding_rate)),
        )
        print(
            f"Reconciled {comparisons} observations, {events} events, {scores} score rows",
            flush=True,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Capture MEXC, Binance, and OKX funding estimates and compare them to settled funding."
    )
    parser.add_argument("--exchanges", default="mexc,binance,okx", help="Comma-separated exchanges: mexc,binance,okx")
    parser.add_argument("--symbols", help="Comma-separated exchange-native symbols. Applies to every selected exchange.")
    parser.add_argument("--max-symbols", type=int, default=0, help="Limit symbols per exchange. 0 means all symbols.")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="Research output directory.")
    parser.add_argument("--loop", action="store_true", help="Run continuously.")
    parser.add_argument("--interval", type=float, default=300.0, help="Seconds between capture passes in loop mode.")
    parser.add_argument("--request-sleep", type=float, default=0.12, help="Sleep after each public REST request.")
    parser.add_argument("--timeout", type=int, default=15, help="HTTP request timeout seconds.")
    parser.add_argument("--settlement-grace-minutes", type=float, default=10.0, help="Wait this long after settlement before reconciling.")
    parser.add_argument("--history-tolerance-minutes", type=float, default=2.0, help="Timestamp tolerance for history matching.")
    parser.add_argument("--rate-tolerance", default="0.00000001", help="Decimal rate tolerance for exact-rate matching.")
    parser.add_argument(
        "--min-abs-basis-funding-rate",
        default="0.005",
        help="Minimum absolute displayed funding rate for basis-adjusted event reports. Decimal rate; 0.005 is 0.5%.",
    )
    parser.add_argument("--no-reconcile", action="store_true", help="Only collect snapshots; do not fetch historical funding.")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if not args.loop:
        run_once(args)
        return

    print(
        f"Funding lock research loop running every {args.interval} seconds. Press Ctrl+C to stop.",
        flush=True,
    )
    while True:
        started = time.time()
        try:
            run_once(args)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            print(f"Funding lock research pass failed: {type(exc).__name__}: {exc}", flush=True)
        elapsed = time.time() - started
        time.sleep(max(args.interval - elapsed, 1.0))


if __name__ == "__main__":
    main()
