# scanner.py

import time
from datetime import datetime, timezone

import config
from config import (
    SCAN_INTERVAL_SECONDS,
    MAX_SYMBOLS_TO_SCAN,
    MAX_ORDERBOOK_SYMBOLS,
    MIN_QUOTE_VOLUME_24H_USDT,
)
from binance_client import BinanceFuturesClient
from db import (
    initialise_database,
    insert_funding_snapshot,
    insert_orderbook_snapshot,
    insert_opportunity_snapshot,
)
from scoring import (
    safe_float,
    calculate_orderbook_metrics,
    calculate_opportunity_score,
)


print("CONFIG FILE LOADED:", config.__file__)
print("MAX_SYMBOLS_TO_SCAN LOADED:", MAX_SYMBOLS_TO_SCAN)
print("MAX_ORDERBOOK_SYMBOLS LOADED:", MAX_ORDERBOOK_SYMBOLS)
print("MIN_QUOTE_VOLUME_24H_USDT LOADED:", MIN_QUOTE_VOLUME_24H_USDT)


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def ms_timestamp_to_minutes_from_now(ms_timestamp):
    if ms_timestamp is None:
        return None

    try:
        now_ms = datetime.now(timezone.utc).timestamp() * 1000
        return max(0, (float(ms_timestamp) - now_ms) / 1000 / 60)
    except (TypeError, ValueError):
        return None


def ms_timestamp_to_utc_string(ms_timestamp):
    if ms_timestamp is None:
        return "None"

    try:
        return datetime.fromtimestamp(
            float(ms_timestamp) / 1000,
            tz=timezone.utc
        ).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(ms_timestamp)


def build_ticker_lookup(tickers):
    lookup = {}

    for ticker in tickers:
        symbol = ticker.get("symbol")
        if not symbol:
            continue

        lookup[symbol] = ticker

    return lookup


def is_usdt_perp_symbol(symbol):
    if not symbol:
        return False

    return symbol.endswith("USDT")


def build_funding_candidate(item, ticker_lookup, timestamp):
    symbol = item.get("symbol")

    mark_price = safe_float(item.get("markPrice"))
    index_price = safe_float(item.get("indexPrice"))
    funding_rate = safe_float(item.get("lastFundingRate"), 0)
    next_funding_time = item.get("nextFundingTime")
    time_to_funding_minutes = ms_timestamp_to_minutes_from_now(next_funding_time)

    premium_pct = None
    if mark_price and index_price:
        premium_pct = ((mark_price - index_price) / index_price) * 100

    ticker = ticker_lookup.get(symbol, {})
    quote_volume_24h = safe_float(ticker.get("quoteVolume"), 0)
    volume_24h = safe_float(ticker.get("volume"), 0)
    price_change_pct = safe_float(ticker.get("priceChangePercent"), 0)

    funding_rate_pct = funding_rate * 100 if funding_rate is not None else None
    absolute_funding_rate_pct = abs(funding_rate_pct) if funding_rate_pct is not None else None

    funding_row = {
        "timestamp": timestamp,
        "exchange": "binance",
        "symbol": symbol,
        "mark_price": mark_price,
        "index_price": index_price,
        "funding_rate": funding_rate,
        "funding_rate_pct": funding_rate_pct,
        "next_funding_time": next_funding_time,
        "time_to_funding_minutes": time_to_funding_minutes,
        "premium_pct": premium_pct,
        "quote_volume_24h": quote_volume_24h,
    }
    insert_funding_snapshot(funding_row)

    return {
        "symbol": symbol,
        "mark_price": mark_price,
        "index_price": index_price,
        "funding_rate": funding_rate,
        "funding_rate_pct": funding_rate_pct,
        "absolute_funding_rate_pct": absolute_funding_rate_pct,
        "next_funding_time": next_funding_time,
        "time_to_funding_minutes": time_to_funding_minutes,
        "premium_pct": premium_pct,
        "quote_volume_24h": quote_volume_24h,
        "volume_24h": volume_24h,
        "price_change_pct": price_change_pct,
    }


def run_scan_once():
    client = BinanceFuturesClient()
    timestamp = utc_now_iso()

    funding_data = client.get_all_funding_snapshots()
    tickers = client.get_24h_tickers()
    ticker_lookup = build_ticker_lookup(tickers)

    usdt_funding_data = [
        item for item in funding_data
        if is_usdt_perp_symbol(item.get("symbol"))
    ]

    symbols_to_scan = (
        usdt_funding_data
        if MAX_SYMBOLS_TO_SCAN is None
        else usdt_funding_data[:MAX_SYMBOLS_TO_SCAN]
    )

    print(f"\n[{timestamp}] Binance futures scan")
    print(f"USDT perpetual symbols found: {len(usdt_funding_data)}")
    print(f"Symbols scanned for funding this run: {len(symbols_to_scan)}")

    funding_candidates = []
    opportunities = []

    # ------------------------------------------------------------
    # Stage 1: funding scan for all selected symbols
    # ------------------------------------------------------------
    for item in symbols_to_scan:
        symbol = item.get("symbol")

        try:
            candidate = build_funding_candidate(
                item=item,
                ticker_lookup=ticker_lookup,
                timestamp=timestamp,
            )

            funding_candidates.append(candidate)

        except Exception as exc:
            print(f"Error scanning funding for {symbol}: {exc}")

    funding_candidates_sorted = sorted(
        funding_candidates,
        key=lambda x: x["absolute_funding_rate_pct"] or 0,
        reverse=True
    )

    liquid_funding_candidates = [
        row for row in funding_candidates_sorted
        if (row["quote_volume_24h"] or 0) >= MIN_QUOTE_VOLUME_24H_USDT
    ]

    orderbook_candidates = liquid_funding_candidates[:MAX_ORDERBOOK_SYMBOLS]

    print(f"Funding candidates collected: {len(funding_candidates)}")
    print(f"Liquid candidates after volume filter: {len(liquid_funding_candidates)}")
    print(f"Order books to fetch this run: {len(orderbook_candidates)}")

    # ------------------------------------------------------------
    # Stage 2: order book scan for top liquid funding names only
    # ------------------------------------------------------------
    for candidate in orderbook_candidates:
        symbol = candidate["symbol"]

        try:
            orderbook = client.get_orderbook_snapshot(symbol=symbol, limit=100)
            metrics = calculate_orderbook_metrics(orderbook)

            if metrics is None:
                print(f"Skipping {symbol}: invalid orderbook")
                continue

            orderbook_row = {
                "timestamp": timestamp,
                "exchange": "binance",
                "symbol": symbol,
                **metrics,
            }
            insert_orderbook_snapshot(orderbook_row)

            score = calculate_opportunity_score(
                funding_rate=candidate["funding_rate"],
                spread_pct=metrics["spread_pct"],
                bid_depth_1pct=metrics["bid_depth_1pct"],
                ask_depth_1pct=metrics["ask_depth_1pct"],
            )

            opportunity_row = {
                "timestamp": timestamp,
                "exchange": "binance",
                "symbol": symbol,
                "time_to_funding_minutes": candidate["time_to_funding_minutes"],
                "spread_pct": metrics["spread_pct"],
                "quote_volume_24h": candidate["quote_volume_24h"],
                **score,
            }
            insert_opportunity_snapshot(opportunity_row)

            opportunities.append(opportunity_row)

        except Exception as exc:
            print(f"Error scanning orderbook for {symbol}: {exc}")

    # ------------------------------------------------------------
    # Table 1: Top 10 opportunities by rough net edge
    # ------------------------------------------------------------
    opportunities = sorted(
        opportunities,
        key=lambda x: x["rough_net_edge_pct"],
        reverse=True
    )

    print("\nTop 10 opportunities by rough net edge:")
    print(
        "Symbol       "
        "Funding %   "
        "Abs Fund %   "
        "Spread %   "
        "Fees %   "
        "Rough Net %   "
        "Liq Score   "
        "Score"
    )

    for row in opportunities[:10]:
        print(
            f"{row['symbol']:<12}"
            f"{row['funding_rate_pct']:>9.4f}   "
            f"{row['absolute_funding_rate_pct']:>10.4f}   "
            f"{row['spread_pct']:>8.4f}   "
            f"{row['estimated_fees_pct']:>6.4f}   "
            f"{row['rough_net_edge_pct']:>11.4f}   "
            f"{row['liquidity_score']:>9.1f}   "
            f"{row['opportunity_score']:>6.2f}"
        )

    # ------------------------------------------------------------
    # Table 2: Top 20 funding opportunities by absolute funding
    # ------------------------------------------------------------
    print("\nTop 20 funding opportunities by absolute funding rate:")
    print(
        "Symbol       "
        "Funding %   "
        "Abs Fund %   "
        "Hours To Funding   "
        "Next Funding UTC        "
        "Premium %   "
        "24h Quote Vol"
    )

    for row in funding_candidates_sorted[:20]:
        print(
            f"{row['symbol']:<12}"
            f"{(row['funding_rate_pct'] or 0):>9.4f}   "
            f"{(row['absolute_funding_rate_pct'] or 0):>10.4f}   "
            f"{((row['time_to_funding_minutes'] or 0) / 60):>16.2f}   "
            f"{ms_timestamp_to_utc_string(row['next_funding_time']):<23}"
            f"{(row['premium_pct'] or 0):>9.4f}   "
            f"{(row['quote_volume_24h'] or 0):>14,.0f}"
        )


def main():
    initialise_database()

    while True:
        run_scan_once()
        time.sleep(SCAN_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()