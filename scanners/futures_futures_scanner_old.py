from __future__ import annotations

import csv
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
from core.orderbook import estimate_execution_from_orderbook
from core.scoring import calculate_net_edge_pct, classify_opportunity


MAX_SYMBOLS = 100
NOTIONALS_USDT = [1_000, 2_500, 5_000]

# Rough taker/taker assumption for opening both futures legs.
# Later we should model maker/taker per exchange and entry + exit separately.
ESTIMATED_FEES_PCT = 0.10

OUTPUT_DIR = REPO_ROOT / "data" / "futures_futures_snapshots"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def pct_diff(numerator_price: float, denominator_price: float) -> float:
    if denominator_price <= 0:
        raise ValueError("denominator_price must be positive")
    return ((numerator_price / denominator_price) - 1) * 100


def ascii_usdt_symbol(symbol: str) -> bool:
    return bool(re.fullmatch(r"[A-Z0-9]+USDT", symbol or ""))


def write_results_to_csv(results: list[dict], timestamp: datetime) -> Path | None:
    if not results:
        return None

    output_file = OUTPUT_DIR / f"binance_bitget_futures_futures_{timestamp.strftime('%Y%m%d')}.csv"

    fieldnames = [
        "timestamp_utc",
        "symbol",
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
        "notional_usdt",
        "net_edge_ex_funding_pct",
        "net_edge_inc_funding_pct",
        "long_fillable",
        "short_fillable",
        "classification",
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
                "symbol": row["symbol"],
                "long_exchange": row["long_exchange"],
                "short_exchange": row["short_exchange"],
                "direction": row["direction"],
                "long_price": row["long_price"],
                "short_price": row["short_price"],
                "gross_spread_pct": row["gross_spread_pct"],
                "long_funding_pct": row["long_funding_pct"],
                "short_funding_pct": row["short_funding_pct"],
                "funding_benefit_pct": row["funding_benefit_pct"],
                "slippage_pct": row["slippage_pct"],
                "fees_pct": row["fees_pct"],
                "notional_usdt":row["notional_usdt"],
                "net_edge_ex_funding_pct":row["net_edge_ex_funding_pct"],
                "net_edge_inc_funding_pct":row["net_edge_inc_funding_pct"],
                "long_fillable":row["long_fillable"],
                "short_fillable":row["short_fillable"],
                "classification": row["classification"],
                "long_next_funding_time_utc": row["long_next_funding_time_utc"],
                "short_next_funding_time_utc": row["short_next_funding_time_utc"],
            })

    return output_file


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
) -> dict | None:
    """
    Direction:
        Long futures on long_exchange at ask.
        Short futures on short_exchange at bid.
    """
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

    if not long_buy.is_fillable or not short_sell.is_fillable:
        return {
            "symbol": symbol,
            "notional_usdt": notional_usdt,
            "long_exchange": long_exchange,
            "short_exchange": short_exchange,
            "direction": f"long_{long_exchange}_short_{short_exchange}",
            "long_price": long_buy.average_price,
            "short_price": short_sell.average_price,
            "gross_spread_pct": None,
            "long_funding_pct": (long_funding.funding_rate or 0) * 100,
            "short_funding_pct": (short_funding.funding_rate or 0) * 100,
            "funding_benefit_pct": None,
            "slippage_pct": None,
            "fees_pct": ESTIMATED_FEES_PCT,
            "net_edge_ex_funding_pct": None,
            "net_edge_inc_funding_pct": None,
            "classification": "NOFILL",
            "long_next_funding_time_utc": long_funding.next_funding_time_utc,
            "short_next_funding_time_utc": short_funding.next_funding_time_utc,
            "long_fillable": long_buy.is_fillable,
            "short_fillable": short_sell.is_fillable,
        }

    gross_spread_pct = pct_diff(
        numerator_price=short_sell.average_price,
        denominator_price=long_buy.average_price,
    )

    long_funding_pct = (long_funding.funding_rate or 0) * 100
    short_funding_pct = (short_funding.funding_rate or 0) * 100

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
        "symbol": symbol,
        "notional_usdt": notional_usdt,
        "long_exchange": long_exchange,
        "short_exchange": short_exchange,
        "direction": f"long_{long_exchange}_short_{short_exchange}",
        "long_price": long_buy.average_price,
        "short_price": short_sell.average_price,
        "gross_spread_pct": gross_spread_pct,
        "long_funding_pct": long_funding_pct,
        "short_funding_pct": short_funding_pct,
        "funding_benefit_pct": funding_benefit_pct,
        "slippage_pct": slippage_pct,
        "fees_pct": ESTIMATED_FEES_PCT,
        "net_edge_ex_funding_pct": net_edge_ex_funding_pct,
        "net_edge_inc_funding_pct": net_edge_inc_funding_pct,
        "classification": classify_opportunity(net_edge_inc_funding_pct),
        "long_next_funding_time_utc": long_funding.next_funding_time_utc,
        "short_next_funding_time_utc": short_funding.next_funding_time_utc,
        "long_fillable": long_buy.is_fillable,
        "short_fillable": short_sell.is_fillable,
    }

def get_common_liquid_symbols(
    binance: BinanceMarketAdapter,
    bitget: BitgetMarketAdapter,
    max_symbols: int | None,
) -> list[str]:
    binance_symbols = set(binance.get_futures_usdt_symbols())
    bitget_symbols = set(bitget.get_liquidity_ranked_futures_symbols(max_symbols=None))

    common = sorted(binance_symbols.intersection(bitget_symbols))
    common = [s for s in common if ascii_usdt_symbol(s)]

    # Rank by Bitget liquidity for now because the Bitget adapter already has
    # a clean futures-volume ranking. Later we can rank by combined Binance + Bitget volume.
    bitget_ranked = bitget.get_liquidity_ranked_futures_symbols(max_symbols=None)
    bitget_rank_lookup = {symbol: idx for idx, symbol in enumerate(bitget_ranked)}

    common = sorted(
        common,
        key=lambda s: bitget_rank_lookup.get(s, 999_999),
    )

    if max_symbols is not None:
        common = common[:max_symbols]

    return common


def scan_binance_bitget_futures_futures():
    timestamp = datetime.now(timezone.utc)

    binance = BinanceMarketAdapter()
    bitget = BitgetMarketAdapter()

    symbols = get_common_liquid_symbols(
        binance=binance,
        bitget=bitget,
        max_symbols=MAX_SYMBOLS,
    )

    results = []

    print(f"\n[{timestamp.isoformat()}] Binance vs Bitget futures-futures scan")
    print(f"Symbols: {len(symbols)} | Notionals: {NOTIONALS_USDT}")
    print("Symbol list ranked by Bitget futures 24h volume")

    for i, symbol in enumerate(symbols, start=1):
        if i == 1 or i % 25 == 0 or i == len(symbols):
            print(f"Progress: {i}/{len(symbols)}")

        try:
            binance_book = binance.get_futures_orderbook(symbol, limit=100)
            bitget_book = bitget.get_futures_orderbook(symbol, limit=100)

            binance_funding = binance.get_funding_info(symbol)
            bitget_funding = bitget.get_funding_info(symbol)

            # Direction A: long Binance, short Bitget
            for notional in NOTIONALS_USDT:
                # Direction A: long Binance, short Bitget
                result_a = build_direction_result(
                    symbol=symbol,
                    long_exchange="binance",
                    short_exchange="bitget",
                    long_orderbook=binance_book,
                    short_orderbook=bitget_book,
                    long_funding=binance_funding,
                    short_funding=bitget_funding,
                    notional_usdt=notional,
                )

                if result_a:
                    results.append(result_a)

                # Direction B: long Bitget, short Binance
                result_b = build_direction_result(
                    symbol=symbol,
                    long_exchange="bitget",
                    short_exchange="binance",
                    long_orderbook=bitget_book,
                    short_orderbook=binance_book,
                    long_funding=bitget_funding,
                    short_funding=binance_funding,
                    notional_usdt=notional,
                )

                if result_b:
                    results.append(result_b)

        except Exception as exc:
            print(f"Error scanning {symbol}: {exc}")

    results = sorted(
        results,
        key=lambda x: x["net_edge_inc_funding_pct"] if x["net_edge_inc_funding_pct"] is not None else -999,
        reverse=True,
    )

    print("\nTop Binance-Bitget futures-futures opportunities")
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

    def fmt(value):
        return "  NOFILL" if value is None else f"{value:>8.4f}"

    for row in results[:50]:
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
    scan_binance_bitget_futures_futures()