# scanner.py

import time
from datetime import datetime, timezone

import config
from config import (
    SCAN_INTERVAL_SECONDS,
    MAX_SYMBOLS_TO_SCAN,
    MAX_ORDERBOOK_SYMBOLS,
    MIN_TURNOVER_24H_USDT,
)
from db import (
    initialise_database,
    upsert_symbol,
    insert_funding_snapshot,
    insert_orderbook_snapshot,
    insert_opportunity_snapshot,
)
from kucoin_client import KuCoinFuturesClient
from scoring import calculate_orderbook_metrics, calculate_opportunity_score, safe_float


print("CONFIG FILE LOADED:", config.__file__)
print("MAX_SYMBOLS_TO_SCAN LOADED:", MAX_SYMBOLS_TO_SCAN)
print("MAX_ORDERBOOK_SYMBOLS LOADED:", MAX_ORDERBOOK_SYMBOLS)
print("MIN_TURNOVER_24H_USDT LOADED:", MIN_TURNOVER_24H_USDT)


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def ms_remaining_to_minutes(ms_remaining):
    if ms_remaining is None:
        return None

    try:
        return max(0, float(ms_remaining) / 1000 / 60)
    except (TypeError, ValueError):
        return None


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


def normalise_symbol(contract):
    return {
        "exchange": "kucoin",
        "symbol": contract.get("symbol"),
        "base_currency": contract.get("baseCurrency"),
        "quote_currency": contract.get("quoteCurrency"),
        "status": contract.get("status"),
        "contract_type": contract.get("type"),
        "multiplier": safe_float(contract.get("multiplier")),
        "max_leverage": safe_float(contract.get("maxLeverage")),
        "tick_size": safe_float(contract.get("tickSize")),
        "lot_size": safe_float(contract.get("lotSize")),
    }


def is_usdt_perp(contract):
    symbol = contract.get("symbol", "")
    quote = contract.get("quoteCurrency", "")
    status = contract.get("status", "")

    return (
        symbol.endswith("USDTM")
        and quote == "USDT"
        and status == "Open"
    )


def build_funding_candidate(client, contract, timestamp):
    symbol = contract.get("symbol")

    mark_price = safe_float(contract.get("markPrice"))
    index_price = safe_float(contract.get("indexPrice"))
    turnover_24h = safe_float(contract.get("turnoverOf24h"), 0)
    volume_24h = safe_float(contract.get("volumeOf24h"), 0)
    open_interest = safe_float(contract.get("openInterest"), 0)

    # Old contract-level field from /contracts/active.
    # Keep only for validation.
    contract_funding_rate = safe_float(contract.get("fundingFeeRate"), 0)

    # Dedicated KuCoin funding endpoint.
    # This matches the KuCoin UI funding rate.
    funding_api_data = client.get_current_funding_rate(symbol)

    funding_rate = safe_float(
        funding_api_data.get("nextFundingRate"),
        contract_funding_rate
    )

    next_funding_time = (
        funding_api_data.get("fundingTime")
        or contract.get("nextFundingRateDateTime")
    )

    time_to_funding_minutes = ms_timestamp_to_minutes_from_now(next_funding_time)

    if time_to_funding_minutes is None:
        time_to_funding_minutes = ms_remaining_to_minutes(
            contract.get("nextFundingRateTime")
        )

    predicted_funding_rate = safe_float(contract.get("predictedFundingFeeRate"))

    funding_rate_granularity_ms = contract.get("fundingRateGranularity")
    current_funding_rate_granularity_ms = contract.get("currentFundingRateGranularity")

    funding_rate_pct = funding_rate * 100 if funding_rate is not None else None
    absolute_funding_rate_pct = abs(funding_rate_pct) if funding_rate_pct is not None else None

    contract_funding_rate_pct = (
        contract_funding_rate * 100
        if contract_funding_rate is not None
        else None
    )

    predicted_funding_rate_pct = (
        predicted_funding_rate * 100
        if predicted_funding_rate is not None
        else None
    )

    funding_row = {
        "timestamp": timestamp,
        "exchange": "kucoin",
        "symbol": symbol,
        "mark_price": mark_price,
        "index_price": index_price,
        "funding_rate": funding_rate,
        "next_funding_time": next_funding_time,
        "time_to_funding_minutes": time_to_funding_minutes,
    }
    insert_funding_snapshot(funding_row)

    return {
        "contract": contract,
        "symbol": symbol,
        "funding_rate": funding_rate,
        "funding_rate_pct": funding_rate_pct,
        "absolute_funding_rate_pct": absolute_funding_rate_pct,
        "contract_funding_rate_pct": contract_funding_rate_pct,
        "predicted_funding_rate_pct": predicted_funding_rate_pct,
        "time_to_funding_minutes": time_to_funding_minutes,
        "next_funding_rate_datetime": next_funding_time,
        "funding_rate_granularity_hours": (
            safe_float(funding_rate_granularity_ms, 0) / 1000 / 60 / 60
        ),
        "current_funding_rate_granularity_hours": (
            safe_float(current_funding_rate_granularity_ms, 0) / 1000 / 60 / 60
        ),
        "funding_api_symbol": funding_api_data.get("symbol"),
        "funding_rate_cap_pct": safe_float(funding_api_data.get("fundingRateCap"), 0) * 100,
        "funding_rate_floor_pct": safe_float(funding_api_data.get("fundingRateFloor"), 0) * 100,
        "mark_price": mark_price,
        "index_price": index_price,
        "turnover_24h": turnover_24h,
        "volume_24h": volume_24h,
        "open_interest": open_interest,
    }


def run_scan_once():
    client = KuCoinFuturesClient()
    timestamp = utc_now_iso()

    contracts = client.get_active_contracts()
    usdt_contracts = [c for c in contracts if is_usdt_perp(c)]

    contracts_to_scan = (
        usdt_contracts
        if MAX_SYMBOLS_TO_SCAN is None
        else usdt_contracts[:MAX_SYMBOLS_TO_SCAN]
    )

    print(f"\n[{timestamp}] KuCoin futures scan")
    print(f"Active USDT perpetual contracts found: {len(usdt_contracts)}")
    print(f"Contracts scanned for funding this run: {len(contracts_to_scan)}")

    funding_candidates = []
    opportunities = []

    # ------------------------------------------------------------
    # Stage 1: scan funding for all selected contracts
    # ------------------------------------------------------------
    for contract in contracts_to_scan:
        symbol = contract.get("symbol")

        try:
            upsert_symbol(normalise_symbol(contract))

            candidate = build_funding_candidate(
                client=client,
                contract=contract,
                timestamp=timestamp,
            )

            funding_candidates.append(candidate)

        except Exception as exc:
            print(f"Error scanning funding for {symbol}: {exc}")

    # ------------------------------------------------------------
    # Stage 2: filter and select only top funding candidates
    # for order book / spread / liquidity scoring
    # ------------------------------------------------------------
    liquid_funding_candidates = [
        row for row in funding_candidates
        if (row["turnover_24h"] or 0) >= MIN_TURNOVER_24H_USDT
    ]

    funding_candidates_sorted = sorted(
        funding_candidates,
        key=lambda x: x["absolute_funding_rate_pct"] or 0,
        reverse=True
    )

    liquid_funding_candidates_sorted = sorted(
        liquid_funding_candidates,
        key=lambda x: x["absolute_funding_rate_pct"] or 0,
        reverse=True
    )

    orderbook_candidates = liquid_funding_candidates_sorted[:MAX_ORDERBOOK_SYMBOLS]

    print(f"Funding candidates collected: {len(funding_candidates)}")
    print(f"Liquid candidates after turnover filter: {len(liquid_funding_candidates)}")
    print(f"Order books to fetch this run: {len(orderbook_candidates)}")

    # ------------------------------------------------------------
    # Stage 3: fetch order books only for top liquid funding names
    # ------------------------------------------------------------
    for candidate in orderbook_candidates:
        symbol = candidate["symbol"]

        try:
            orderbook = client.get_orderbook_snapshot(symbol)
            metrics = calculate_orderbook_metrics(orderbook)

            if metrics is None:
                print(f"Skipping {symbol}: invalid orderbook")
                continue

            orderbook_row = {
                "timestamp": timestamp,
                "exchange": "kucoin",
                "symbol": symbol,
                **metrics,
            }
            insert_orderbook_snapshot(orderbook_row)

            score = calculate_opportunity_score(
                funding_rate=candidate["funding_rate"],
                time_to_funding_minutes=candidate["time_to_funding_minutes"],
                spread_pct=metrics["spread_pct"],
                bid_depth_1pct=metrics["bid_depth_1pct"],
                ask_depth_1pct=metrics["ask_depth_1pct"],
            )

            opportunity_row = {
                "timestamp": timestamp,
                "exchange": "kucoin",
                "symbol": symbol,
                "time_to_funding_minutes": candidate["time_to_funding_minutes"],
                "spread_pct": metrics["spread_pct"],
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
    # Table 2: Top 10 funding opportunities by absolute funding
    # This includes all funding candidates, not just orderbook candidates.
    # ------------------------------------------------------------
    print("\nTop 10 funding opportunities by absolute funding rate:")
    print(
        "Symbol       "
        "Funding %   "
        "Abs Fund %   "
        "Contract %   "
        "Predicted %   "
        "Hours To Funding   "
        "Next Funding UTC        "
        "Cycle Hrs   "
        "24h Turnover"
    )

    for row in funding_candidates_sorted[:10]:
        next_funding_utc = ms_timestamp_to_utc_string(
            row["next_funding_rate_datetime"]
        )

        predicted = row["predicted_funding_rate_pct"]
        contract_funding = row["contract_funding_rate_pct"]

        print(
            f"{row['symbol']:<12}"
            f"{(row['funding_rate_pct'] or 0):>9.4f}   "
            f"{(row['absolute_funding_rate_pct'] or 0):>10.4f}   "
            f"{contract_funding if contract_funding is not None else 0:>10.4f}   "
            f"{predicted if predicted is not None else 0:>11.4f}   "
            f"{((row['time_to_funding_minutes'] or 0) / 60):>16.2f}   "
            f"{next_funding_utc:<23}"
            f"{row['funding_rate_granularity_hours']:>8.2f}   "
            f"{(row['turnover_24h'] or 0):>12,.0f}"
        )


def main():
    initialise_database()

    while True:
        run_scan_once()
        time.sleep(SCAN_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()