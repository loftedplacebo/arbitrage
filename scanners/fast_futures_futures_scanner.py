from __future__ import annotations

import argparse
import csv
import itertools
import re
import sys
import time
import statistics
from collections import defaultdict, deque
from pathlib import Path
from datetime import datetime, timezone


# Allow running from repo root or directly from scanners folder
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from Binance.binance_market_adapter import BinanceMarketAdapter
from Bitget.bitget_market_adapter import BitgetMarketAdapter
from Mexc.mexc_market_adapter import MexcMarketAdapter
from Kucoin.kucoin_market_adapter import KucoinMarketAdapter
from Hyperliquid.hyperliquid_market_adapter import HyperliquidMarketAdapter

from core.orderbook import estimate_execution_from_orderbook
from core.scoring import calculate_net_edge_pct, classify_opportunity


# -----------------------------
# Fast scan config
# -----------------------------
EXPERIMENT_ID = "spread_ladder_20260616_v1"
FAST_SPREAD_THRESHOLD_PCT = 0.08
MIN_COMBINED_VOLUME_USDT = 1_000_000
MAX_FAST_CANDIDATES = 500
FAST_SCAN_CRYPTO_ONLY = False
ML_OBSERVATION_LOGGING_ENABLED = True
ML_FAST_SPREAD_LOG_THRESHOLD_PCT = 0.03
ML_MAX_FAST_OBSERVATIONS = 2_000
ROUTE_STATS_HISTORY_SCANS = 720
ROUTE_STATS_BOOTSTRAP_MAX_ROWS = 200_000


# -----------------------------
# Deep validation config
# -----------------------------
DEEP_VALIDATE_TOP_N = 150
DEEP_VALIDATE_CRYPTO_ONLY = True
NOTIONALS_USDT = [100, 200, 300, 400, 500, 1_000, 2_500]

# Rough taker/taker assumption for opening both futures legs only.
# Later we should model entry + exit and exchange-specific maker/taker fees.
ESTIMATED_FEES_PCT = 0.10


# -----------------------------
# Persistence / paper-readiness config
# -----------------------------
PERSISTENCE_WINDOW_SCANS = 3
MIN_PERSISTENCE_COUNT = 2

MIN_NET_EX_FUNDING_FOR_READY_PCT = 0.10
MIN_NET_INC_FUNDING_FOR_READY_PCT = 0.20

# If true, paper-ready flag will only apply to crypto-class instruments.
CRYPTO_ONLY_READY = True


# -----------------------------
# Instrument classification
# -----------------------------
TOKENISED_STOCK_KEYWORDS = [
    "OPENAI",
    "STOCK",
    "TQQQ",
    "LLY",
    "QCOM",
    "AMD",
    "SOXL",
    "ARM",
    "AAOI",
    "SPCX",
    "XOM",
    "NATGAS",
    "XAU",
    "XAUT",
    "PAXG",
]

TOKENISED_STOCK_BASE_SYMBOLS = {
    "ANTHROPIC",
    "APLD",
    "BP",
    "OKLO",
    "QNTSTOCK",
    "RDDT",
    "SOXS",
    "SQQQ",
}

# Keep this permissive for now. We can expand once real examples appear.
UNKNOWN_OR_SPECIAL_KEYWORDS = [
    "USDCUSDT",
]


# -----------------------------
# Output
# -----------------------------
FAST_OUTPUT_DIR = REPO_ROOT / "data" / "fast_futures_futures_snapshots"
FAST_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

DEEP_OUTPUT_DIR = REPO_ROOT / "data" / "validated_futures_futures_snapshots"
DEEP_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

ML_OUTPUT_DIR = REPO_ROOT / "data" / "ml" / "fast_spread_observations"
ML_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


# -----------------------------
# Helpers
# -----------------------------
def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def ascii_usdt_symbol(symbol: str) -> bool:
    return bool(re.fullmatch(r"[A-Z0-9]+USDT", symbol or ""))


def pct_diff(numerator_price: float, denominator_price: float) -> float:
    if denominator_price <= 0:
        raise ValueError("denominator_price must be positive")
    return ((numerator_price / denominator_price) - 1) * 100


def fmt(value):
    return " NOFILL" if value is None else f"{value:>8.4f}"


def classify_instrument(symbol: str) -> str:
    symbol = symbol.upper()
    base_symbol = symbol.removesuffix("USDT")

    if base_symbol in TOKENISED_STOCK_BASE_SYMBOLS:
        return "tokenised_stock_or_synthetic"

    for keyword in TOKENISED_STOCK_KEYWORDS:
        if keyword in symbol:
            return "tokenised_stock_or_synthetic"

    for keyword in UNKNOWN_OR_SPECIAL_KEYWORDS:
        if keyword in symbol:
            return "unknown_or_special"

    return "crypto"


def persistence_key(row: dict) -> str:
    return (
        f"{row['symbol']}|"
        f"{row['direction']}|"
        f"{int(row['notional_usdt'])}"
    )


def candidate_key(row: dict) -> str:
    return f"{row['symbol']}|{row['direction']}"


def route_key(row: dict) -> str:
    return (
        f"{row['symbol']}|"
        f"{row['long_exchange']}|"
        f"{row['short_exchange']}|"
        f"{row['direction']}"
    )


def calculate_route_stats(history: deque, current_spread_pct: float | None) -> dict:
    values = [float(value) for value in history if value is not None]
    if current_spread_pct is None or not values:
        return {
            "route_observation_count": len(values),
            "route_spread_mean_pct": None,
            "route_spread_median_pct": None,
            "route_spread_min_pct": None,
            "route_spread_max_pct": None,
            "route_spread_std_pct": None,
            "route_spread_zscore": None,
            "route_spread_percentile": None,
            "route_spread_trend_pct": None,
        }

    mean = statistics.fmean(values)
    median = statistics.median(values)
    min_value = min(values)
    max_value = max(values)
    std = statistics.pstdev(values) if len(values) > 1 else 0.0
    zscore = (current_spread_pct - mean) / std if std > 1e-9 else 0.0
    percentile = sum(1 for value in values if value <= current_spread_pct) / len(values)
    recent_values = values[-min(5, len(values)):]
    trend = current_spread_pct - statistics.fmean(recent_values)

    return {
        "route_observation_count": len(values),
        "route_spread_mean_pct": mean,
        "route_spread_median_pct": median,
        "route_spread_min_pct": min_value,
        "route_spread_max_pct": max_value,
        "route_spread_std_pct": std,
        "route_spread_zscore": zscore,
        "route_spread_percentile": percentile,
        "route_spread_trend_pct": trend,
    }


def annotate_route_stats(rows: list[dict], route_spread_history: dict[str, deque]) -> list[dict]:
    for row in rows:
        stats = calculate_route_stats(
            route_spread_history.get(route_key(row), deque(maxlen=ROUTE_STATS_HISTORY_SCANS)),
            row.get("fast_spread_pct"),
        )
        row.update(stats)
    return rows


def update_route_spread_history(rows: list[dict], route_spread_history: dict[str, deque]) -> None:
    for row in rows:
        spread = row.get("fast_spread_pct")
        if spread is None:
            continue
        route_spread_history.setdefault(
            route_key(row),
            deque(maxlen=ROUTE_STATS_HISTORY_SCANS),
        ).append(float(spread))


def bootstrap_route_spread_history() -> dict[str, deque]:
    history: dict[str, deque] = {}
    files = sorted(ML_OUTPUT_DIR.glob("fast_spread_observations_*.csv"), reverse=True)
    rows = []
    remaining = ROUTE_STATS_BOOTSTRAP_MAX_ROWS

    for path in files:
        if remaining <= 0:
            break
        try:
            with path.open("r", newline="", encoding="utf-8") as f:
                file_rows = list(csv.DictReader(f))
        except OSError:
            continue
        rows.extend(file_rows[-remaining:])
        remaining = ROUTE_STATS_BOOTSTRAP_MAX_ROWS - len(rows)

    rows.sort(key=lambda row: row.get("timestamp_utc", ""))
    for row in rows[-ROUTE_STATS_BOOTSTRAP_MAX_ROWS:]:
        try:
            row["fast_spread_pct"] = float(row.get("fast_spread_pct") or 0)
        except (TypeError, ValueError):
            continue
        update_route_spread_history([row], history)

    return history


# -----------------------------
# Fast scan
# -----------------------------
def build_fast_candidates(ticker_data: dict[str, dict[str, dict]]) -> list[dict]:
    """
    Build fast cross-exchange futures candidates using ticker bid/ask only.

    Direction:
        Long futures on exchange A at ask.
        Short futures on exchange B at bid.

    This does not fetch order books. It only finds apparent top-of-book spreads.
    """
    candidates = []

    exchange_pairs = list(itertools.combinations(ticker_data.keys(), 2))

    for exchange_a, exchange_b in exchange_pairs:
        tickers_a = ticker_data[exchange_a]
        tickers_b = ticker_data[exchange_b]

        common_symbols = sorted(set(tickers_a).intersection(tickers_b))
        common_symbols = [s for s in common_symbols if ascii_usdt_symbol(s)]

        for symbol in common_symbols:
            a = tickers_a[symbol]
            b = tickers_b[symbol]

            combined_volume = (a.get("volume_usdt") or 0) + (b.get("volume_usdt") or 0)
            if combined_volume < MIN_COMBINED_VOLUME_USDT:
                continue

            instrument_class = classify_instrument(symbol)
            if FAST_SCAN_CRYPTO_ONLY and instrument_class != "crypto":
                continue

            # Direction A:
            # Long A at ask, short B at bid.
            spread_ab = pct_diff(
                numerator_price=b["bid"],
                denominator_price=a["ask"],
            )

            if spread_ab >= FAST_SPREAD_THRESHOLD_PCT:
                candidates.append({
                    "symbol": symbol,
                    "instrument_class": instrument_class,
                    "long_exchange": exchange_a,
                    "short_exchange": exchange_b,
                    "direction": f"long_{exchange_a}_short_{exchange_b}",
                    "long_ask": a["ask"],
                    "short_bid": b["bid"],
                    "fast_spread_pct": spread_ab,
                    "long_volume_usdt": a.get("volume_usdt"),
                    "short_volume_usdt": b.get("volume_usdt"),
                    "combined_volume_usdt": combined_volume,
                })

            # Direction B:
            # Long B at ask, short A at bid.
            spread_ba = pct_diff(
                numerator_price=a["bid"],
                denominator_price=b["ask"],
            )

            if spread_ba >= FAST_SPREAD_THRESHOLD_PCT:
                candidates.append({
                    "symbol": symbol,
                    "instrument_class": instrument_class,
                    "long_exchange": exchange_b,
                    "short_exchange": exchange_a,
                    "direction": f"long_{exchange_b}_short_{exchange_a}",
                    "long_ask": b["ask"],
                    "short_bid": a["bid"],
                    "fast_spread_pct": spread_ba,
                    "long_volume_usdt": b.get("volume_usdt"),
                    "short_volume_usdt": a.get("volume_usdt"),
                    "combined_volume_usdt": combined_volume,
                })

    candidates = sorted(
        candidates,
        key=lambda x: x["fast_spread_pct"],
        reverse=True,
    )

    return candidates[:MAX_FAST_CANDIDATES]


def build_fast_observations(ticker_data: dict[str, dict[str, dict]]) -> list[dict]:
    """
    Build a broader low-threshold fast-spread observation set for analysis.

    These rows are not strategy candidates and are not deep-validated. They
    preserve short-lived top-of-book spread observations for later research.
    """
    observations = []

    exchange_pairs = list(itertools.combinations(ticker_data.keys(), 2))

    for exchange_a, exchange_b in exchange_pairs:
        tickers_a = ticker_data[exchange_a]
        tickers_b = ticker_data[exchange_b]

        common_symbols = sorted(set(tickers_a).intersection(tickers_b))
        common_symbols = [s for s in common_symbols if ascii_usdt_symbol(s)]

        for symbol in common_symbols:
            a = tickers_a[symbol]
            b = tickers_b[symbol]

            combined_volume = (a.get("volume_usdt") or 0) + (b.get("volume_usdt") or 0)
            if combined_volume < MIN_COMBINED_VOLUME_USDT:
                continue

            instrument_class = classify_instrument(symbol)
            if FAST_SCAN_CRYPTO_ONLY and instrument_class != "crypto":
                continue

            spread_ab = pct_diff(
                numerator_price=b["bid"],
                denominator_price=a["ask"],
            )
            if spread_ab >= ML_FAST_SPREAD_LOG_THRESHOLD_PCT:
                observations.append({
                    "symbol": symbol,
                    "instrument_class": instrument_class,
                    "long_exchange": exchange_a,
                    "short_exchange": exchange_b,
                    "direction": f"long_{exchange_a}_short_{exchange_b}",
                    "long_bid": a["bid"],
                    "long_ask": a["ask"],
                    "short_bid": b["bid"],
                    "short_ask": b["ask"],
                    "fast_spread_pct": spread_ab,
                    "long_volume_usdt": a.get("volume_usdt"),
                    "short_volume_usdt": b.get("volume_usdt"),
                    "combined_volume_usdt": combined_volume,
                })

            spread_ba = pct_diff(
                numerator_price=a["bid"],
                denominator_price=b["ask"],
            )
            if spread_ba >= ML_FAST_SPREAD_LOG_THRESHOLD_PCT:
                observations.append({
                    "symbol": symbol,
                    "instrument_class": instrument_class,
                    "long_exchange": exchange_b,
                    "short_exchange": exchange_a,
                    "direction": f"long_{exchange_b}_short_{exchange_a}",
                    "long_bid": b["bid"],
                    "long_ask": b["ask"],
                    "short_bid": a["bid"],
                    "short_ask": a["ask"],
                    "fast_spread_pct": spread_ba,
                    "long_volume_usdt": b.get("volume_usdt"),
                    "short_volume_usdt": a.get("volume_usdt"),
                    "combined_volume_usdt": combined_volume,
                })

    observations = sorted(
        observations,
        key=lambda x: x["fast_spread_pct"],
        reverse=True,
    )

    return observations[:ML_MAX_FAST_OBSERVATIONS]


def build_observation_id(timestamp: datetime, row: dict) -> str:
    return (
        f"{timestamp.isoformat()}|"
        f"{row['symbol']}|"
        f"{row['long_exchange']}|"
        f"{row['short_exchange']}|"
        f"{row['direction']}"
    )


def write_fast_observations_to_csv(observations: list[dict], timestamp: datetime) -> Path | None:
    if not observations:
        return None

    output_file = ML_OUTPUT_DIR / f"fast_spread_observations_{timestamp.strftime('%Y%m%d')}.csv"

    fieldnames = [
        "timestamp_utc",
        "experiment_id",
        "observation_id",
        "symbol",
        "instrument_class",
        "long_exchange",
        "short_exchange",
        "direction",
        "long_bid",
        "long_ask",
        "short_bid",
        "short_ask",
        "fast_spread_pct",
        "long_volume_usdt",
        "short_volume_usdt",
        "combined_volume_usdt",
        "config_fast_spread_threshold_pct",
        "config_ml_fast_spread_log_threshold_pct",
        "config_min_combined_volume_usdt",
        "config_max_fast_candidates",
        "config_deep_validate_top_n",
        "config_deep_validate_crypto_only",
        "config_fast_scan_crypto_only",
        "config_ml_max_fast_observations",
    ]

    file_exists = output_file.exists()
    if file_exists:
        with output_file.open("r", newline="", encoding="utf-8") as f:
            existing_header = next(csv.reader(f), [])
        if existing_header != fieldnames:
            output_file = ML_OUTPUT_DIR / f"fast_spread_observations_{timestamp.strftime('%Y%m%d_%H%M%S')}.csv"
            file_exists = output_file.exists()

    with output_file.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)

        if not file_exists:
            writer.writeheader()

        for row in observations:
            writer.writerow({
                "timestamp_utc": timestamp.isoformat(),
                "experiment_id": EXPERIMENT_ID,
                "observation_id": build_observation_id(timestamp, row),
                "config_fast_spread_threshold_pct": FAST_SPREAD_THRESHOLD_PCT,
                "config_ml_fast_spread_log_threshold_pct": ML_FAST_SPREAD_LOG_THRESHOLD_PCT,
                "config_min_combined_volume_usdt": MIN_COMBINED_VOLUME_USDT,
                "config_max_fast_candidates": MAX_FAST_CANDIDATES,
                "config_deep_validate_top_n": DEEP_VALIDATE_TOP_N,
                "config_deep_validate_crypto_only": DEEP_VALIDATE_CRYPTO_ONLY,
                "config_fast_scan_crypto_only": FAST_SCAN_CRYPTO_ONLY,
                "config_ml_max_fast_observations": ML_MAX_FAST_OBSERVATIONS,
                **row,
            })

    return output_file


def write_fast_candidates_to_csv(candidates: list[dict], timestamp: datetime) -> Path | None:
    if not candidates:
        return None

    output_file = FAST_OUTPUT_DIR / f"fast_futures_futures_{timestamp.strftime('%Y%m%d')}.csv"

    fieldnames = [
        "timestamp_utc",
        "experiment_id",
        "symbol",
        "instrument_class",
        "long_exchange",
        "short_exchange",
        "direction",
        "long_ask",
        "short_bid",
        "fast_spread_pct",
        "long_volume_usdt",
        "short_volume_usdt",
        "combined_volume_usdt",
    ]

    file_exists = output_file.exists()
    if file_exists:
        with output_file.open("r", newline="", encoding="utf-8") as f:
            existing_header = next(csv.reader(f), [])
        if existing_header != fieldnames:
            output_file = FAST_OUTPUT_DIR / f"fast_futures_futures_{timestamp.strftime('%Y%m%d_%H%M%S')}.csv"
            file_exists = output_file.exists()

    with output_file.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)

        if not file_exists:
            writer.writeheader()

        for row in candidates:
            output_row = {
                "timestamp_utc": timestamp.isoformat(),
                "experiment_id": EXPERIMENT_ID,
                **row,
            }
            writer.writerow({field: output_row.get(field) for field in fieldnames})

    return output_file


# -----------------------------
# Deep validation
# -----------------------------
def deep_validate_candidate(
    candidate: dict,
    adapters: dict,
    timestamp: datetime,
) -> list[dict]:
    """
    Deep-validate one fast candidate using real order books and funding.

    The fast candidate tells us:
        long_exchange
        short_exchange
        symbol

    This function checks executable prices at multiple notionals.
    """
    symbol = candidate["symbol"]
    long_exchange = candidate["long_exchange"]
    short_exchange = candidate["short_exchange"]

    long_adapter = adapters[long_exchange]
    short_adapter = adapters[short_exchange]

    results = []

    long_orderbook = long_adapter.get_futures_orderbook(symbol, limit=100)
    short_orderbook = short_adapter.get_futures_orderbook(symbol, limit=100)

    long_funding = long_adapter.get_funding_info(symbol)
    short_funding = short_adapter.get_funding_info(symbol)

    for notional in NOTIONALS_USDT:
        long_buy = estimate_execution_from_orderbook(
            orderbook=long_orderbook,
            side="buy",
            notional_usdt=notional,
        )

        short_sell = estimate_execution_from_orderbook(
            orderbook=short_orderbook,
            side="sell",
            notional_usdt=notional,
        )

        long_close_sell = estimate_execution_from_orderbook(
            orderbook=long_orderbook,
            side="sell",
            notional_usdt=notional,
        )

        short_close_buy = estimate_execution_from_orderbook(
            orderbook=short_orderbook,
            side="buy",
            notional_usdt=notional,
        )

        long_funding_pct = (long_funding.funding_rate or 0) * 100
        short_funding_pct = (short_funding.funding_rate or 0) * 100

        base_row = {
            "timestamp_utc": timestamp.isoformat(),
            "symbol": symbol,
            "instrument_class": candidate["instrument_class"],
            "notional_usdt": notional,
            "long_exchange": long_exchange,
            "short_exchange": short_exchange,
            "direction": candidate["direction"],
            "fast_spread_pct": candidate["fast_spread_pct"],
            "fast_long_ask": candidate["long_ask"],
            "fast_short_bid": candidate["short_bid"],
            "combined_volume_usdt": candidate["combined_volume_usdt"],
            "long_funding_pct": long_funding_pct,
            "short_funding_pct": short_funding_pct,
            "long_next_funding_time_utc": long_funding.next_funding_time_utc,
            "short_next_funding_time_utc": short_funding.next_funding_time_utc,
            "long_fillable": long_buy.is_fillable,
            "short_fillable": short_sell.is_fillable,
            "long_close_avg_price": long_close_sell.average_price,
            "short_close_avg_price": short_close_buy.average_price,
            "long_close_fillable": long_close_sell.is_fillable,
            "short_close_fillable": short_close_buy.is_fillable,
            "close_slippage_pct": (
                long_close_sell.slippage_pct + short_close_buy.slippage_pct
                if long_close_sell.is_fillable and short_close_buy.is_fillable
                else None
            ),
            "route_observation_count": candidate.get("route_observation_count"),
            "route_spread_mean_pct": candidate.get("route_spread_mean_pct"),
            "route_spread_median_pct": candidate.get("route_spread_median_pct"),
            "route_spread_min_pct": candidate.get("route_spread_min_pct"),
            "route_spread_max_pct": candidate.get("route_spread_max_pct"),
            "route_spread_std_pct": candidate.get("route_spread_std_pct"),
            "route_spread_zscore": candidate.get("route_spread_zscore"),
            "route_spread_percentile": candidate.get("route_spread_percentile"),
            "route_spread_trend_pct": candidate.get("route_spread_trend_pct"),
        }

        if not long_buy.is_fillable or not short_sell.is_fillable:
            results.append({
                **base_row,
                "long_avg_price": long_buy.average_price,
                "short_avg_price": short_sell.average_price,
                "validated_spread_pct": None,
                "funding_benefit_pct": None,
                "slippage_pct": None,
                "fees_pct": ESTIMATED_FEES_PCT,
                "net_edge_ex_funding_pct": None,
                "net_edge_inc_funding_pct": None,
                "classification": "NOFILL",
                "persistence_count": 0,
                "persistent": False,
                "spread_ready": False,
                "funding_adjusted_ready": False,
                "paper_ready": False,
            })
            continue

        validated_spread_pct = pct_diff(
            numerator_price=short_sell.average_price,
            denominator_price=long_buy.average_price,
        )

        funding_benefit_pct = short_funding_pct - long_funding_pct
        slippage_pct = long_buy.slippage_pct + short_sell.slippage_pct

        net_edge_ex_funding_pct = calculate_net_edge_pct(
            gross_spread_pct=validated_spread_pct,
            estimated_fees_pct=ESTIMATED_FEES_PCT,
            estimated_slippage_pct=slippage_pct,
            expected_funding_pct=0.0,
        )

        net_edge_inc_funding_pct = calculate_net_edge_pct(
            gross_spread_pct=validated_spread_pct,
            estimated_fees_pct=ESTIMATED_FEES_PCT,
            estimated_slippage_pct=slippage_pct,
            expected_funding_pct=funding_benefit_pct,
        )

        results.append({
            **base_row,
            "long_avg_price": long_buy.average_price,
            "short_avg_price": short_sell.average_price,
            "validated_spread_pct": validated_spread_pct,
            "funding_benefit_pct": funding_benefit_pct,
            "slippage_pct": slippage_pct,
            "fees_pct": ESTIMATED_FEES_PCT,
            "net_edge_ex_funding_pct": net_edge_ex_funding_pct,
            "net_edge_inc_funding_pct": net_edge_inc_funding_pct,
            "classification": classify_opportunity(net_edge_inc_funding_pct),
            "persistence_count": 0,
            "persistent": False,
            "spread_ready": False,
            "funding_adjusted_ready": False,
            "paper_ready": False,
        })

    return results


def apply_persistence_and_readiness(
    validated_results: list[dict],
    persistence_history: dict[str, deque],
) -> list[dict]:
    """
    Tracks whether a symbol/direction/notional appears as positive in recent scans.

    We now separate:
        spread_ready:
            Spread-only trade is persistent and positive before funding adjustment.

        funding_adjusted_ready:
            Trade remains persistent and positive after funding adjustment.

        paper_ready:
            For now, paper trading should only use funding-adjusted-ready trades.
            This avoids entering trades where funding turns the economics negative.
    """
    current_positive_keys = set()

    for row in validated_results:
        net_ex = row.get("net_edge_ex_funding_pct")
        net_inc = row.get("net_edge_inc_funding_pct")

        spread_positive = (
            net_ex is not None
            and net_ex >= MIN_NET_EX_FUNDING_FOR_READY_PCT
        )

        funding_adjusted_positive = (
            net_inc is not None
            and net_inc >= MIN_NET_INC_FUNDING_FOR_READY_PCT
        )

        if spread_positive or funding_adjusted_positive:
            current_positive_keys.add(persistence_key(row))

    # Update each seen row key history
    all_keys = set(persistence_history.keys()).union(current_positive_keys)

    for key in all_keys:
        persistence_history.setdefault(
            key,
            deque(maxlen=PERSISTENCE_WINDOW_SCANS),
        )
        persistence_history[key].append(key in current_positive_keys)

    for row in validated_results:
        key = persistence_key(row)
        history = persistence_history.get(key, deque(maxlen=PERSISTENCE_WINDOW_SCANS))
        persistence_count = sum(1 for value in history if value)
        persistent = persistence_count >= MIN_PERSISTENCE_COUNT

        net_ex = row.get("net_edge_ex_funding_pct")
        net_inc = row.get("net_edge_inc_funding_pct")

        spread_edge_ok = (
            net_ex is not None
            and net_ex >= MIN_NET_EX_FUNDING_FOR_READY_PCT
        )

        funding_adjusted_edge_ok = (
            net_inc is not None
            and net_inc >= MIN_NET_INC_FUNDING_FOR_READY_PCT
        )

        instrument_ok = (
            row.get("instrument_class") == "crypto"
            if CRYPTO_ONLY_READY
            else True
        )

        fillable_ok = (
            row.get("long_fillable") is True
            and row.get("short_fillable") is True
        )

        spread_ready = bool(
            spread_edge_ok
            and persistent
            and instrument_ok
            and fillable_ok
        )

        funding_adjusted_ready = bool(
            funding_adjusted_edge_ok
            and persistent
            and instrument_ok
            and fillable_ok
        )

        # Conservative paper-trading rule:
        # only enter when still positive after funding adjustment.
        paper_ready = funding_adjusted_ready

        row["persistence_count"] = persistence_count
        row["persistent"] = persistent
        row["spread_ready"] = spread_ready
        row["funding_adjusted_ready"] = funding_adjusted_ready
        row["paper_ready"] = paper_ready

    return validated_results

def build_capacity_summary(validated_results: list[dict]) -> list[dict]:
    """
    Summarise maximum notional that remains positive for each symbol/direction.

    Uses net_edge_inc_funding_pct for now, but also reports ex-funding capacity.
    """
    grouped = defaultdict(list)

    for row in validated_results:
        grouped[candidate_key(row)].append(row)

    summaries = []

    for key, rows in grouped.items():
        rows = sorted(rows, key=lambda x: x["notional_usdt"])

        positive_inc = [
            row["notional_usdt"]
            for row in rows
            if row.get("net_edge_inc_funding_pct") is not None
            and row["net_edge_inc_funding_pct"] > 0
        ]

        positive_ex = [
            row["notional_usdt"]
            for row in rows
            if row.get("net_edge_ex_funding_pct") is not None
            and row["net_edge_ex_funding_pct"] > 0
        ]

        best_row = max(
            rows,
            key=lambda x: x["net_edge_inc_funding_pct"] if x["net_edge_inc_funding_pct"] is not None else -999,
        )

        summaries.append({
            "symbol": best_row["symbol"],
            "instrument_class": best_row["instrument_class"],
            "direction": best_row["direction"],
            "max_positive_notional_inc_funding": max(positive_inc) if positive_inc else 0,
            "max_positive_notional_ex_funding": max(positive_ex) if positive_ex else 0,
            "best_net_edge_inc_funding_pct": best_row["net_edge_inc_funding_pct"],
            "best_net_edge_ex_funding_pct": best_row["net_edge_ex_funding_pct"],
            "spread_ready_any": any(row.get("spread_ready") for row in rows),
            "funding_adjusted_ready_any": any(row.get("funding_adjusted_ready") for row in rows),
            "paper_ready_any": any(row.get("paper_ready") for row in rows),
        })

    summaries = sorted(
        summaries,
        key=lambda x: (
            x["best_net_edge_inc_funding_pct"]
            if x["best_net_edge_inc_funding_pct"] is not None
            else -999
        ),
        reverse=True,
    )

    return summaries


def write_validated_results_to_csv(results: list[dict], timestamp: datetime) -> Path | None:
    if not results:
        return None

    output_file = DEEP_OUTPUT_DIR / f"validated_futures_futures_{timestamp.strftime('%Y%m%d')}.csv"

    fieldnames = [
        "timestamp_utc",
        "experiment_id",
        "symbol",
        "instrument_class",
        "notional_usdt",
        "long_exchange",
        "short_exchange",
        "direction",
        "fast_spread_pct",
        "fast_long_ask",
        "fast_short_bid",
        "long_avg_price",
        "short_avg_price",
        "long_close_avg_price",
        "short_close_avg_price",
        "validated_spread_pct",
        "long_funding_pct",
        "short_funding_pct",
        "funding_benefit_pct",
        "slippage_pct",
        "close_slippage_pct",
        "route_observation_count",
        "route_spread_mean_pct",
        "route_spread_median_pct",
        "route_spread_min_pct",
        "route_spread_max_pct",
        "route_spread_std_pct",
        "route_spread_zscore",
        "route_spread_percentile",
        "route_spread_trend_pct",
        "fees_pct",
        "net_edge_ex_funding_pct",
        "net_edge_inc_funding_pct",
        "classification",
        "long_fillable",
        "short_fillable",
        "long_close_fillable",
        "short_close_fillable",
        "persistence_count",
        "persistent",
        "spread_ready",
        "funding_adjusted_ready",
        "paper_ready",
        "combined_volume_usdt",
        "long_next_funding_time_utc",
        "short_next_funding_time_utc",
    ]

    file_exists = output_file.exists()
    if file_exists:
        with output_file.open("r", newline="", encoding="utf-8") as f:
            existing_header = next(csv.reader(f), [])
        if existing_header != fieldnames:
            output_file = DEEP_OUTPUT_DIR / f"validated_futures_futures_{timestamp.strftime('%Y%m%d_%H%M%S')}.csv"
            file_exists = output_file.exists()

    with output_file.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)

        if not file_exists:
            writer.writeheader()

        for row in results:
            output_row = {"experiment_id": EXPERIMENT_ID, **row}
            writer.writerow(output_row)

    return output_file


# -----------------------------
# Main scan cycle
# -----------------------------
def run_scan_once(
    adapters: dict,
    persistence_history: dict[str, deque],
    route_spread_history: dict[str, deque],
) -> tuple[list[dict], list[dict]]:
    timestamp = utc_now()

    print(f"\n[{timestamp.isoformat()}] Fast futures-futures spread scan")
    print(f"Threshold: {FAST_SPREAD_THRESHOLD_PCT:.4f}%")
    print(f"Min combined volume: ${MIN_COMBINED_VOLUME_USDT:,.0f}")
    print(f"Max fast candidates: {MAX_FAST_CANDIDATES}")
    print(f"Deep validate top N: {DEEP_VALIDATE_TOP_N}")
    print(f"Fast scan crypto only: {FAST_SCAN_CRYPTO_ONLY}")
    print(f"Deep validate crypto only: {DEEP_VALIDATE_CRYPTO_ONLY}")
    print(f"ML observation logging enabled: {ML_OBSERVATION_LOGGING_ENABLED}")
    print(f"ML fast spread threshold: {ML_FAST_SPREAD_LOG_THRESHOLD_PCT:.4f}%")

    ticker_data = {}

    for name, adapter in adapters.items():
        try:
            tickers = adapter.get_fast_futures_tickers()
            ticker_data[name] = tickers
            print(f"{name:<8} tickers: {len(tickers)}")
        except Exception as exc:
            print(f"Error fetching fast tickers for {name}: {exc}")

    observations = []
    if ML_OBSERVATION_LOGGING_ENABLED:
        observations = build_fast_observations(ticker_data)
        ml_output_file = write_fast_observations_to_csv(observations, timestamp)
        if ml_output_file:
            print(f"Wrote {len(observations)} ML fast spread observations to {ml_output_file}")
        else:
            print("No ML fast spread observations found.")

    candidates = build_fast_candidates(ticker_data)
    candidates = annotate_route_stats(candidates, route_spread_history)
    update_route_spread_history(observations, route_spread_history)

    print("\nTop fast spread candidates")
    print(
        "Symbol       "
        "Class                    "
        "Direction                    "
        "Spread %   "
        "Long Ask      "
        "Short Bid     "
        "Combined Vol"
    )

    for row in candidates[:30]:
        print(
            f"{row['symbol']:<12}"
            f"{row['instrument_class']:<25}"
            f"{row['direction']:<29}"
            f"{row['fast_spread_pct']:>8.4f}  "
            f"{row['long_ask']:>12.6f}  "
            f"{row['short_bid']:>12.6f}  "
            f"${row['combined_volume_usdt']:>14,.0f}"
        )

    fast_output_file = write_fast_candidates_to_csv(candidates, timestamp)

    if fast_output_file:
        print(f"\nWrote {len(candidates)} fast candidates to {fast_output_file}")
    else:
        print("\nNo fast candidates found.")

    validated_results = []
    deep_candidate_pool = candidates
    non_crypto_fast_candidates = [
        candidate
        for candidate in candidates
        if candidate.get("instrument_class") != "crypto"
    ]

    if DEEP_VALIDATE_CRYPTO_ONLY:
        deep_candidate_pool = [
            candidate
            for candidate in candidates
            if candidate.get("instrument_class") == "crypto"
        ]
        print(
            f"\nSkipped {len(non_crypto_fast_candidates)} non-crypto fast candidates "
            "from deep validation."
        )

    deep_candidates = deep_candidate_pool[:DEEP_VALIDATE_TOP_N]

    print(
        f"\nDeep-validating {len(deep_candidates)} candidates "
        f"from {len(candidates)} fast candidates "
        f"(crypto_only={DEEP_VALIDATE_CRYPTO_ONLY})..."
    )

    for i, candidate in enumerate(deep_candidates, start=1):
        try:
            print(
                f"  Deep {i}/{len(deep_candidates)}: "
                f"{candidate['symbol']} {candidate['direction']} "
                f"fast_spread={candidate['fast_spread_pct']:.4f}%"
            )

            rows = deep_validate_candidate(
                candidate=candidate,
                adapters=adapters,
                timestamp=timestamp,
            )
            validated_results.extend(rows)

        except Exception as exc:
            print(
                f"  Error deep-validating {candidate['symbol']} "
                f"{candidate['direction']}: {exc}"
            )

    validated_results = sorted(
        validated_results,
        key=lambda x: (
            x["net_edge_ex_funding_pct"]
            if x["net_edge_ex_funding_pct"] is not None
            else -999,
            x["net_edge_inc_funding_pct"]
            if x["net_edge_inc_funding_pct"] is not None
            else -999,
        ),
        reverse=True,
    )

    validated_results = apply_persistence_and_readiness(
        validated_results,
        persistence_history,
    )

    capacity_summary = build_capacity_summary(validated_results)

    print("\nTop deep-validated futures-futures opportunities")
    print(
    "Symbol       "
    "Class                    "
    "Notional   "
    "Direction                    "
    "Fast %    "
    "Valid %   "
    "FundAdj % "
    "Slip %    "
    "Net exF % "
    "Net incF % "
    "Persist "
    "SprdR "
    "FundR "
    "Ready "
    "Class"
)

    for row in validated_results[:30]:
        print(
            f"{row['symbol']:<12}"
            f"{row['instrument_class']:<25}"
            f"${row['notional_usdt']:<9,.0f}"
            f"{row['direction']:<29}"
            f"{fmt(row['fast_spread_pct'])}  "
            f"{fmt(row['validated_spread_pct'])}  "
            f"{fmt(row['funding_benefit_pct'])}  "
            f"{fmt(row['slippage_pct'])}  "
            f"{fmt(row['net_edge_ex_funding_pct'])}  "
            f"{fmt(row['net_edge_inc_funding_pct'])}  "
            f"{row['persistence_count']:<8}"
            f"{str(row.get('spread_ready')):<6}"
            f"{str(row.get('funding_adjusted_ready')):<6}"
            f"{str(row.get('paper_ready')):<6}"
            f"{row['classification']:<10}"
        )

    print("\nCapacity summary")
    print(
    "Symbol       "
    "Class                    "
    "Direction                    "
    "Max incF "
    "Max exF  "
    "Best incF "
    "Best exF  "
    "SprdR "
    "FundR "
    "Ready"
)

    for row in capacity_summary[:20]:
        print(
            f"{row['symbol']:<12}"
            f"{row['instrument_class']:<25}"
            f"{row['direction']:<29}"
            f"${row['max_positive_notional_inc_funding']:<8,.0f}"
            f"${row['max_positive_notional_ex_funding']:<8,.0f}"
            f"{fmt(row['best_net_edge_inc_funding_pct'])}  "
            f"{fmt(row['best_net_edge_ex_funding_pct'])}  "
            f"{row['paper_ready_any']}"
        )

    validated_output_file = write_validated_results_to_csv(
        validated_results,
        timestamp,
    )

    if validated_output_file:
        print(f"\nWrote {len(validated_results)} validated rows to {validated_output_file}")
    else:
        print("\nNo validated rows written.")

    return candidates, validated_results


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fast futures-futures scanner with deep validation")
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Run continuously",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=30,
        help="Seconds between loop scans",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    adapters = {
        "binance": BinanceMarketAdapter(),
        "bitget": BitgetMarketAdapter(),
        "mexc": MexcMarketAdapter(),
        "kucoin": KucoinMarketAdapter(),
        "hyperliquid": HyperliquidMarketAdapter(),
    }

    persistence_history: dict[str, deque] = {}
    route_spread_history = bootstrap_route_spread_history()
    print(
        "Loaded route spread history for "
        f"{len(route_spread_history)} routes from ML observations."
    )

    if not args.loop:
        run_scan_once(
            adapters=adapters,
            persistence_history=persistence_history,
            route_spread_history=route_spread_history,
        )
        return

    print(f"Running in loop mode every {args.interval} seconds. Press Ctrl+C to stop.")

    while True:
        try:
            run_scan_once(
                adapters=adapters,
                persistence_history=persistence_history,
                route_spread_history=route_spread_history,
            )
            time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\nStopped by user.")
            break
        except Exception as exc:
            print(f"\nLoop error: {exc}")
            time.sleep(args.interval)


if __name__ == "__main__":
    main()
