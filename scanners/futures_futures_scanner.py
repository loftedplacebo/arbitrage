from __future__ import annotations

import csv
import itertools
import re
import sys
from pathlib import Path
from datetime import datetime, timezone

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from Binance.binance_market_adapter import BinanceMarketAdapter
from Bitget.bitget_market_adapter import BitgetMarketAdapter
from Mexc.mexc_market_adapter import MexcMarketAdapter
from Kucoin.kucoin_market_adapter import KucoinMarketAdapter

from core.orderbook import estimate_execution_from_orderbook
from core.scoring import calculate_net_edge_pct, classify_opportunity


MAX_SYMBOLS_PER_PAIR = 50
NOTIONALS_USDT = [1_000, 2_500, 5_000]
ESTIMATED_FEES_PCT = 0.10

OUTPUT_DIR = REPO_ROOT / "data" / "futures_futures_snapshots"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def pct_diff(numerator_price: float, denominator_price: float) -> float:
    if denominator_price <= 0:
        raise ValueError("denominator_price must be positive")
    return ((numerator_price / denominator_price) - 1) * 100


def ascii_usdt_symbol(symbol: str) -> bool:
    return bool(re.fullmatch(r"[A-Z0-9]+USDT", symbol or ""))


def safe_ranked_symbols(adapter, max_symbols: int | None = None) -> list[str]:
    if hasattr(adapter, "get_liquidity_ranked_futures_symbols"):
        return adapter.get_liquidity_ranked_futures_symbols(max_symbols=max_symbols)

    symbols = sorted(adapter.get_futures_usdt_symbols())
    symbols = [s for s in symbols if ascii_usdt_symbol(s)]

    if max_symbols is not None:
        symbols = symbols[:max_symbols]

    return symbols


def get_common_symbols(adapter_a, adapter_b, max_symbols: int | None) -> list[str]:
    symbols_a = set(adapter_a.get_futures_usdt_symbols())
    symbols_b = set(adapter_b.get_futures_usdt_symbols())

    common = sorted(symbols_a.intersection(symbols_b))
    common = [s for s in common if ascii_usdt_symbol(s)]

    # Rank by adapter A liquidity where available, then keep only common symbols.
    ranked_a = safe_ranked_symbols(adapter_a, max_symbols=None)
    rank_a = {symbol: idx for idx, symbol in enumerate(ranked_a)}

    ranked_b = safe_ranked_symbols(adapter_b, max_symbols=None)
    rank_b = {symbol: idx for idx, symbol in enumerate(ranked_b)}

    common = sorted(
        common,
        key=lambda s: min(rank_a.get(s, 999_999), rank_b.get(s, 999_999)),
    )

    if max_symbols is not None:
        common = common[:max_symbols]

    return common


def build_direction_result(
    *,
    symbol: str,
    long_exchange: str,
    short_exchange: str,
    long_orderbook,
    short_orderbook,
    long_funding,
    short_funding,
    notional_usdt: float,
) -> dict:
    long_buy = estimate_execution_from_orderbook(
        orderbook=long_orderbook,
        side="buy",
        notional_usdt=notional_usdt,
    )

    short_sell = estimate_execution_from_orderbook(
        orderbook=short_orderbook,
        side="sell",
        notional_usdt=notional_usdt,
    )

    long_funding_pct = (long_funding.funding_rate or 0) * 100
    short_funding_pct = (short_funding.funding_rate or 0) * 100

    base = {
        "symbol": symbol,
        "notional_usdt": notional_usdt,
        "long_exchange": long_exchange,
        "short_exchange": short_exchange,
        "exchange_pair": f"{long_exchange}-{short_exchange}",
        "direction": f"long_{long_exchange}_short_{short_exchange}",
        "long_price": long_buy.average_price,
        "short_price": short_sell.average_price,
        "long_funding_pct": long_funding_pct,
        "short_funding_pct": short_funding_pct,
        "fees_pct": ESTIMATED_FEES_PCT,
        "long_next_funding_time_utc": long_funding.next_funding_time_utc,
        "short_next_funding_time_utc": short_funding.next_funding_time_utc,
        "long_fillable": long_buy.is_fillable,
        "short_fillable": short_sell.is_fillable,
    }

    if not long_buy.is_fillable or not short_sell.is_fillable:
        return {
            **base,
            "gross_spread_pct": None,
            "funding_benefit_pct": None,
            "slippage_pct": None,
            "net_edge_ex_funding_pct": None,
            "net_edge_inc_funding_pct": None,
            "classification": "NOFILL",
        }

    gross_spread_pct = pct_diff(
        numerator_price=short_sell.average_price,
        denominator_price=long_buy.average_price,
    )

    # Long receives when funding is negative.
    # Short receives when funding is positive.
    funding_benefit_pct = short_funding_pct - long_funding_pct

    slippage_pct = long_buy.slippage_pct + short_sell.slippage_pct

    net_edge_ex_funding_pct = calculate_net_edge_pct(
        gross_spread_pct=gross_spread_pct,
        estimated_fees_pct=ESTIMATED_FEES_PCT,
        estimated_slippage_pct=slippage_pct,
        expected_funding_pct=0.0,
    )

    net_edge_inc_funding_pct = calculate_net_edge_pct(
        gross_spread_pct=gross_spread_pct,
        estimated_fees_pct=ESTIMATED_FEES_PCT,
        estimated_slippage_pct=slippage_pct,
        expected_funding_pct=funding_benefit_pct,
    )

    return {
        **base,
        "gross_spread_pct": gross_spread_pct,
        "funding_benefit_pct": funding_benefit_pct,
        "slippage_pct": slippage_pct,
        "net_edge_ex_funding_pct": net_edge_ex_funding_pct,
        "net_edge_inc_funding_pct": net_edge_inc_funding_pct,
        "classification": classify_opportunity(net_edge_inc_funding_pct),
    }


def write_results_to_csv(results: list[dict], timestamp: datetime) -> Path | None:
    if not results:
        return None

    output_file = OUTPUT_DIR / f"multi_exchange_futures_futures_{timestamp.strftime('%Y%m%d')}.csv"

    fieldnames = [
        "timestamp_utc",
        "symbol",
        "notional_usdt",
        "exchange_pair",
        "long_exchange",
        "short_exchange",
        "direction",
        "long_price",
        "short_price",
        "gross_spread_pct",
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
        "long_next_funding_time_utc",
        "short_next_funding_time_utc",
    ]

    file_exists = output_file.exists()

    with output_file.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)

        if not file_exists:
            writer.writeheader()

        for row in results:
            writer.writerow({
                "timestamp_utc": timestamp.isoformat(),
                **row,
            })

    return output_file


def fmt(value):
    return "  NOFILL" if value is None else f"{value:>8.4f}"


def scan_multi_exchange_futures_futures():
    timestamp = datetime.now(timezone.utc)

    adapters = {
        "binance": BinanceMarketAdapter(),
        "bitget": BitgetMarketAdapter(),
        "mexc": MexcMarketAdapter(),
        "kucoin": KucoinMarketAdapter(),
    }

    results = []

    print(f"\n[{timestamp.isoformat()}] Multi-exchange futures-futures scan")
    print(f"Exchanges: {list(adapters.keys())}")
    print(f"Notionals: {NOTIONALS_USDT}")

    exchange_pairs = list(itertools.combinations(adapters.items(), 2))

    for (exchange_a, adapter_a), (exchange_b, adapter_b) in exchange_pairs:
        print(f"\nScanning pair: {exchange_a} vs {exchange_b}")

        symbols = get_common_symbols(
            adapter_a=adapter_a,
            adapter_b=adapter_b,
            max_symbols=MAX_SYMBOLS_PER_PAIR,
        )

        print(f"Common symbols scanned: {len(symbols)}")

        for i, symbol in enumerate(symbols, start=1):
            if i == 1 or i % 25 == 0 or i == len(symbols):
                print(f"  Progress {exchange_a}-{exchange_b}: {i}/{len(symbols)}")

            try:
                book_a = adapter_a.get_futures_orderbook(symbol, limit=100)
                book_b = adapter_b.get_futures_orderbook(symbol, limit=100)

                funding_a = adapter_a.get_funding_info(symbol)
                funding_b = adapter_b.get_funding_info(symbol)

                for notional in NOTIONALS_USDT:
                    result_ab = build_direction_result(
                        symbol=symbol,
                        long_exchange=exchange_a,
                        short_exchange=exchange_b,
                        long_orderbook=book_a,
                        short_orderbook=book_b,
                        long_funding=funding_a,
                        short_funding=funding_b,
                        notional_usdt=notional,
                    )
                    results.append(result_ab)

                    result_ba = build_direction_result(
                        symbol=symbol,
                        long_exchange=exchange_b,
                        short_exchange=exchange_a,
                        long_orderbook=book_b,
                        short_orderbook=book_a,
                        long_funding=funding_b,
                        short_funding=funding_a,
                        notional_usdt=notional,
                    )
                    results.append(result_ba)

            except Exception as exc:
                print(f"  Error scanning {symbol} on {exchange_a}-{exchange_b}: {exc}")

    results = sorted(
        results,
        key=lambda x: x["net_edge_inc_funding_pct"] if x["net_edge_inc_funding_pct"] is not None else -999,
        reverse=True,
    )

    print("\nTop multi-exchange futures-futures opportunities")
    print(
        "Symbol       "
        "Notional   "
        "Direction                    "
        "Spread %   "
        "FundAdj %  "
        "Slip %    "
        "Fees %    "
        "Net exF % "
        "Net incF % "
        "Class"
    )

    for row in results[:75]:
        print(
            f"{row['symbol']:<12}"
            f"${row['notional_usdt']:<9,.0f}"
            f"{row['direction']:<29}"
            f"{fmt(row['gross_spread_pct'])}  "
            f"{fmt(row['funding_benefit_pct'])}  "
            f"{fmt(row['slippage_pct'])}  "
            f"{row['fees_pct']:>7.4f}  "
            f"{fmt(row['net_edge_ex_funding_pct'])}  "
            f"{fmt(row['net_edge_inc_funding_pct'])}  "
            f"{row['classification']:<10}"
        )

    output_file = write_results_to_csv(results, timestamp)
    if output_file:
        print(f"\nWrote {len(results)} rows to {output_file}")

    return results


if __name__ == "__main__":
    scan_multi_exchange_futures_futures()