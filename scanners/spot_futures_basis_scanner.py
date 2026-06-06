from __future__ import annotations

import sys
from pathlib import Path
from datetime import datetime, timezone
import csv
import re

# Allow running from repo root or directly from scanners folder
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from Binance.binance_market_adapter import BinanceMarketAdapter
from core.orderbook import estimate_execution_from_orderbook
from core.scoring import calculate_net_edge_pct, classify_opportunity


MAX_SYMBOLS = 100
NOTIONAL_USDT = 1_000

# Rough taker/taker assumption for opening both legs only.
# Later we should model entry + exit separately.
ESTIMATED_FEES_PCT = 0.10

OUTPUT_DIR = REPO_ROOT / "data" / "binance_basis_snapshots"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def pct_diff(numerator_price: float, denominator_price: float) -> float:
    if denominator_price <= 0:
        raise ValueError("denominator_price must be positive")
    return ((numerator_price / denominator_price) - 1) * 100

def write_results_to_csv(results: list[dict], timestamp: datetime) -> Path | None:
    if not results:
        return None

    output_file = OUTPUT_DIR / f"binance_spot_futures_basis_{timestamp.strftime('%Y%m%d')}.csv"

    fieldnames = [
        "timestamp_utc",
        "exchange",
        "symbol",
        "direction",
        "spot_price",
        "futures_price",
        "gross_basis_pct",
        "funding_pct",
        "funding_benefit_pct",
        "slippage_pct",
        "fees_pct",
        "net_edge_pct",
        "classification",
        "tradeable_now",
        "next_funding_time_utc",
    ]

    file_exists = output_file.exists()

    with output_file.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)

        if not file_exists:
            writer.writeheader()

        for row in results:
            writer.writerow({
                "timestamp_utc": timestamp.isoformat(),
                "exchange": "binance",
                "symbol": row["symbol"],
                "direction": row["direction"],
                "spot_price": row["spot_price"],
                "futures_price": row["futures_price"],
                "gross_basis_pct": row["gross_basis_pct"],
                "funding_pct": row["funding_pct"],
                "funding_benefit_pct": row["funding_benefit_pct"],
                "slippage_pct": row["slippage_pct"],
                "fees_pct": row["fees_pct"],
                "net_edge_pct": row["net_edge_pct"],
                "classification": row["classification"],
                "tradeable_now": row["tradeable_now"],
                "next_funding_time_utc": row["next_funding_time_utc"],
            })

    return output_file

def scan_binance_spot_futures():
    adapter = BinanceMarketAdapter()
    results = []
    timestamp = datetime.now(timezone.utc)

    symbols = adapter.get_liquidity_ranked_common_symbols(max_symbols=MAX_SYMBOLS)

    # Keep initial scanner simple: standard ASCII USDT symbols only.
    symbols = [
        s for s in symbols
        if re.fullmatch(r"[A-Z0-9]+USDT", s)
    ]

    print(f"\n[{timestamp.isoformat()}] Binance spot-futures basis scan")
    print(f"Symbols: {len(symbols)} | Notional: ${NOTIONAL_USDT:,.0f}")
    print("Symbol list ranked by combined spot + futures 24h quote volume")

    for symbol in symbols:
        try:
            spot_book = adapter.get_spot_orderbook(symbol, limit=100)
            futures_book = adapter.get_futures_orderbook(symbol, limit=100)
            funding = adapter.get_funding_info(symbol)

            # Direction A:
            # Buy spot, short futures.
            # Good when futures bid > spot ask.
            spot_buy = estimate_execution_from_orderbook(
                orderbook=spot_book,
                side="buy",
                notional_usdt=NOTIONAL_USDT,
            )

            futures_sell = estimate_execution_from_orderbook(
                orderbook=futures_book,
                side="sell",
                notional_usdt=NOTIONAL_USDT,
            )

            if spot_buy.is_fillable and futures_sell.is_fillable:
                gross_basis_pct = pct_diff(
                    numerator_price=futures_sell.average_price,
                    denominator_price=spot_buy.average_price,
                )

                funding_pct = (funding.funding_rate or 0) * 100

                # If short futures and funding is positive, we receive funding.
                expected_funding_pct = funding_pct

                slippage_pct = spot_buy.slippage_pct + futures_sell.slippage_pct

                net_edge_pct = calculate_net_edge_pct(
                    gross_spread_pct=gross_basis_pct,
                    estimated_fees_pct=ESTIMATED_FEES_PCT,
                    estimated_slippage_pct=slippage_pct,
                    expected_funding_pct=expected_funding_pct,
                )

                results.append({
                    "symbol": symbol,
                    "direction": "spot_long_futures_short",
                    "spot_price": spot_buy.average_price,
                    "futures_price": futures_sell.average_price,
                    "gross_basis_pct": gross_basis_pct,
                    "funding_pct": funding_pct,
                    "funding_benefit_pct": expected_funding_pct,
                    "slippage_pct": slippage_pct,
                    "fees_pct": ESTIMATED_FEES_PCT,
                    "net_edge_pct": net_edge_pct,
                    "classification": classify_opportunity(net_edge_pct),
                    "next_funding_time_utc": funding.next_funding_time_utc,
                    "tradeable_now": True,
                })

            # Direction B:
            # Long futures, sell/short spot.
            # Good when spot bid > futures ask.
            # This is analysis-only until we model spot borrow/margin.
            spot_sell = estimate_execution_from_orderbook(
                orderbook=spot_book,
                side="sell",
                notional_usdt=NOTIONAL_USDT,
            )

            futures_buy = estimate_execution_from_orderbook(
                orderbook=futures_book,
                side="buy",
                notional_usdt=NOTIONAL_USDT,
            )

            if spot_sell.is_fillable and futures_buy.is_fillable:
                gross_reverse_basis_pct = pct_diff(
                    numerator_price=spot_sell.average_price,
                    denominator_price=futures_buy.average_price,
                )

                funding_pct = (funding.funding_rate or 0) * 100

                # If long futures and funding is positive, we pay funding.
                # If funding is negative, we receive funding.
                expected_funding_pct = -funding_pct

                slippage_pct = spot_sell.slippage_pct + futures_buy.slippage_pct

                net_edge_pct = calculate_net_edge_pct(
                    gross_spread_pct=gross_reverse_basis_pct,
                    estimated_fees_pct=ESTIMATED_FEES_PCT,
                    estimated_slippage_pct=slippage_pct,
                    expected_funding_pct=expected_funding_pct,
                )

                results.append({
                    "symbol": symbol,
                    "direction": "futures_long_spot_short",
                    "spot_price": spot_sell.average_price,
                    "futures_price": futures_buy.average_price,
                    "gross_basis_pct": gross_reverse_basis_pct,
                    "funding_pct": funding_pct,
                    "funding_benefit_pct": expected_funding_pct,
                    "slippage_pct": slippage_pct,
                    "fees_pct": ESTIMATED_FEES_PCT,
                    "net_edge_pct": net_edge_pct,
                    "classification": classify_opportunity(net_edge_pct),
                    "next_funding_time_utc": funding.next_funding_time_utc,
                    "tradeable_now": False,
                })

        except Exception as exc:
            print(f"Error scanning {symbol}: {exc}")

    results = sorted(results, key=lambda x: x["net_edge_pct"], reverse=True)

    print("\nTop Binance spot-futures basis opportunities")
    print(
        "Symbol       "
        "Direction                  "
        "Basis %    "
        "Funding %  "
        "FundAdj %  "
        "Slip %    "
        "Fees %    "
        "Net %     "
        "Class      "
        "Tradeable"
    )

    for row in results[:50]:
        print(
            f"{row['symbol']:<12}"
            f"{row['direction']:<27}"
            f"{row['gross_basis_pct']:>8.4f}  "
            f"{row['funding_pct']:>9.4f}  "
            f"{row['funding_benefit_pct']:>9.4f}  "
            f"{row['slippage_pct']:>7.4f}  "
            f"{row['fees_pct']:>7.4f}  "
            f"{row['net_edge_pct']:>7.4f}  "
            f"{row['classification']:<10}"
            f"{row['tradeable_now']}"
        )
    output_file = write_results_to_csv(results, timestamp)
    if output_file:
        print(f"\nWrote {len(results)} rows to {output_file}")

    return results


if __name__ == "__main__":
    scan_binance_spot_futures()