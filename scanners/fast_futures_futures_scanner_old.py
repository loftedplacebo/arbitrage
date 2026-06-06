from __future__ import annotations

import csv
import itertools
import re
import sys
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

from core.orderbook import estimate_execution_from_orderbook
from core.scoring import calculate_net_edge_pct, classify_opportunity

# Fast scan thresholds
FAST_SPREAD_THRESHOLD_PCT = 0.12
MIN_COMBINED_VOLUME_USDT = 1_000_000
MAX_FAST_CANDIDATES = 50

DEEP_VALIDATE_TOP_N = 20
NOTIONALS_USDT = [1_000, 2_500, 5_000]
ESTIMATED_FEES_PCT = 0.10

DEEP_OUTPUT_DIR = REPO_ROOT / "data" / "validated_futures_futures_snapshots"
DEEP_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


OUTPUT_DIR = REPO_ROOT / "data" / "fast_futures_futures_snapshots"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def ascii_usdt_symbol(symbol: str) -> bool:
    return bool(re.fullmatch(r"[A-Z0-9]+USDT", symbol or ""))


def pct_diff(numerator_price: float, denominator_price: float) -> float:
    if denominator_price <= 0:
        raise ValueError("denominator_price must be positive")
    return ((numerator_price / denominator_price) - 1) * 100


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

            # Direction A:
            # Long A at ask, short B at bid.
            spread_ab = pct_diff(
                numerator_price=b["bid"],
                denominator_price=a["ask"],
            )

            if spread_ab >= FAST_SPREAD_THRESHOLD_PCT:
                candidates.append({
                    "symbol": symbol,
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


def write_candidates_to_csv(candidates: list[dict], timestamp: datetime) -> Path | None:
    if not candidates:
        return None

    output_file = OUTPUT_DIR / f"fast_futures_futures_{timestamp.strftime('%Y%m%d')}.csv"

    fieldnames = [
        "timestamp_utc",
        "symbol",
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

    with output_file.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)

        if not file_exists:
            writer.writeheader()

        for row in candidates:
            writer.writerow({
                "timestamp_utc": timestamp.isoformat(),
                **row,
            })

    return output_file

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

        long_funding_pct = (long_funding.funding_rate or 0) * 100
        short_funding_pct = (short_funding.funding_rate or 0) * 100

        base_row = {
            "timestamp_utc": timestamp.isoformat(),
            "symbol": symbol,
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
        })

    return results

def write_validated_results_to_csv(results: list[dict], timestamp: datetime) -> Path | None:
    if not results:
        return None

    output_file = DEEP_OUTPUT_DIR / f"validated_futures_futures_{timestamp.strftime('%Y%m%d')}.csv"

    fieldnames = [
        "timestamp_utc",
        "symbol",
        "notional_usdt",
        "long_exchange",
        "short_exchange",
        "direction",
        "fast_spread_pct",
        "fast_long_ask",
        "fast_short_bid",
        "long_avg_price",
        "short_avg_price",
        "validated_spread_pct",
        "long_funding_pct",
        "short_funding_pct",
        "funding_benefit_pct",
        "slippage_pct",
        "fees_pct",
        "net_edge_ex_funding_pct",
        "net_edge_inc_funding_pct",
        "classification",
        "long_fillable",
        "short_fillable",
        "combined_volume_usdt",
        "long_next_funding_time_utc",
        "short_next_funding_time_utc",
    ]

    file_exists = output_file.exists()

    with output_file.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)

        if not file_exists:
            writer.writeheader()

        writer.writerows(results)

    return output_file

def main():
    timestamp = datetime.now(timezone.utc)

    adapters = {
        "binance": BinanceMarketAdapter(),
        "bitget": BitgetMarketAdapter(),
        "mexc": MexcMarketAdapter(),
        "kucoin": KucoinMarketAdapter(),
    }

    print(f"\n[{timestamp.isoformat()}] Fast futures-futures spread scan")
    print(f"Threshold: {FAST_SPREAD_THRESHOLD_PCT:.4f}%")
    print(f"Min combined volume: ${MIN_COMBINED_VOLUME_USDT:,.0f}")

    ticker_data = {}

    for name, adapter in adapters.items():
        try:
            tickers = adapter.get_fast_futures_tickers()
            ticker_data[name] = tickers
            print(f"{name:<8} tickers: {len(tickers)}")
        except Exception as exc:
            print(f"Error fetching fast tickers for {name}: {exc}")

    candidates = build_fast_candidates(ticker_data)

    print("\nTop fast spread candidates")
    print(
        "Symbol       "
        "Direction                    "
        "Spread %   "
        "Long Ask      "
        "Short Bid     "
        "Combined Vol"
    )

    for row in candidates[:30]:
        print(
            f"{row['symbol']:<12}"
            f"{row['direction']:<29}"
            f"{row['fast_spread_pct']:>8.4f}  "
            f"{row['long_ask']:>12.6f}  "
            f"{row['short_bid']:>12.6f}  "
            f"${row['combined_volume_usdt']:>14,.0f}"
        )

    output_file = write_candidates_to_csv(candidates, timestamp)

    if output_file:
        print(f"\nWrote {len(candidates)} fast candidates to {output_file}")
    else:
        print("\nNo fast candidates found.")

    validated_results = []

    deep_candidates = candidates[:DEEP_VALIDATE_TOP_N]

    print(f"\nDeep-validating top {len(deep_candidates)} fast candidates...")

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
            x["net_edge_inc_funding_pct"]
            if x["net_edge_inc_funding_pct"] is not None
            else -999
        ),
        reverse=True,
    )

    print("\nTop deep-validated futures-futures opportunities")
    print(
        "Symbol       "
        "Notional   "
        "Direction                    "
        "Fast %    "
        "Valid %   "
        "FundAdj % "
        "Slip %    "
        "Net exF % "
        "Net incF % "
        "Class"
    )

    def fmt(value):
        return " NOFILL" if value is None else f"{value:>8.4f}"

    for row in validated_results[:30]:
        print(
            f"{row['symbol']:<12}"
            f"${row['notional_usdt']:<9,.0f}"
            f"{row['direction']:<29}"
            f"{fmt(row['fast_spread_pct'])}  "
            f"{fmt(row['validated_spread_pct'])}  "
            f"{fmt(row['funding_benefit_pct'])}  "
            f"{fmt(row['slippage_pct'])}  "
            f"{fmt(row['net_edge_ex_funding_pct'])}  "
            f"{fmt(row['net_edge_inc_funding_pct'])}  "
            f"{row['classification']:<10}"
        )

    validated_output_file = write_validated_results_to_csv(
        validated_results,
        timestamp,
    )

    if validated_output_file:
        print(f"\nWrote {len(validated_results)} validated rows to {validated_output_file}")
    else:
        print("\nNo validated rows written.")


if __name__ == "__main__":
    main()