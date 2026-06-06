# calendar_spread_scan.py

import requests
from datetime import datetime, timezone


BASE_URL = "https://fapi.binance.com"

# Keep wide for discovery first.
MIN_ABS_SPREAD_PCT = 0.05
MIN_ABS_ANNUALISED_BASIS_PCT = 1.0

# Relevant contract types for spread scanning.
CONTRACT_TYPES_TO_INCLUDE = {
    "PERPETUAL",
    "CURRENT_MONTH",
    "NEXT_MONTH",
    "CURRENT_QUARTER",
    "NEXT_QUARTER",
}


def safe_float(value, default=None):
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def get_exchange_info():
    response = requests.get(f"{BASE_URL}/fapi/v1/exchangeInfo", timeout=20)
    response.raise_for_status()
    return response.json()


def get_premium_index_all():
    response = requests.get(f"{BASE_URL}/fapi/v1/premiumIndex", timeout=20)
    response.raise_for_status()
    return response.json()


def ms_to_datetime(ms_value):
    if ms_value is None:
        return None

    try:
        return datetime.fromtimestamp(
            int(ms_value) / 1000,
            tz=timezone.utc
        )
    except Exception:
        return None


def days_between(start_dt, end_dt):
    if start_dt is None or end_dt is None:
        return None

    seconds = (end_dt - start_dt).total_seconds()
    return seconds / 86400


def get_pair_key(symbol_info):
    """
    Binance exchangeInfo usually includes pair/baseAsset/quoteAsset.
    For BTCUSDT_250925, pair should generally be BTCUSDT.
    """
    return (
        symbol_info.get("pair")
        or f"{symbol_info.get('baseAsset')}{symbol_info.get('quoteAsset')}"
    )


def build_symbol_metadata():
    exchange_info = get_exchange_info()
    symbols = exchange_info.get("symbols", [])

    metadata = {}

    for item in symbols:
        symbol = item.get("symbol")
        status = item.get("status")
        contract_type = item.get("contractType")

        if not symbol:
            continue

        if status != "TRADING":
            continue

        if contract_type not in CONTRACT_TYPES_TO_INCLUDE:
            continue

        pair_key = get_pair_key(item)

        metadata[symbol] = {
            "symbol": symbol,
            "pair_key": pair_key,
            "base_asset": item.get("baseAsset"),
            "quote_asset": item.get("quoteAsset"),
            "contract_type": contract_type,
            "delivery_date": item.get("deliveryDate"),
            "onboard_date": item.get("onboardDate"),
            "delivery_dt": ms_to_datetime(item.get("deliveryDate")),
        }

    return metadata


def build_price_lookup():
    data = get_premium_index_all()

    price_lookup = {}

    for item in data:
        symbol = item.get("symbol")
        if not symbol:
            continue

        mark_price = safe_float(item.get("markPrice"))
        index_price = safe_float(item.get("indexPrice"))

        if mark_price is None or mark_price <= 0:
            continue

        price_lookup[symbol] = {
            "symbol": symbol,
            "mark_price": mark_price,
            "index_price": index_price,
            "last_funding_rate": safe_float(item.get("lastFundingRate")),
            "next_funding_time": item.get("nextFundingTime"),
        }

    return price_lookup


def group_contracts_by_pair(metadata, price_lookup):
    grouped = {}

    for symbol, meta in metadata.items():
        price = price_lookup.get(symbol)

        if not price:
            continue

        pair_key = meta["pair_key"]

        grouped.setdefault(pair_key, [])

        grouped[pair_key].append({
            **meta,
            **price,
        })

    return grouped


def contract_sort_key(contract):
    """
    Sort perpetual first, then dated contracts by delivery date.
    """
    if contract["contract_type"] == "PERPETUAL":
        return 0

    delivery_dt = contract.get("delivery_dt")
    if delivery_dt is None:
        return 9999999999999

    return int(delivery_dt.timestamp())


def calculate_spread_rows(grouped):
    now = datetime.now(timezone.utc)
    rows = []

    for pair_key, contracts in grouped.items():
        if len(contracts) < 2:
            continue

        contracts = sorted(contracts, key=contract_sort_key)

        for i in range(len(contracts)):
            for j in range(i + 1, len(contracts)):
                near = contracts[i]
                far = contracts[j]

                near_price = near["mark_price"]
                far_price = far["mark_price"]

                if near_price <= 0 or far_price <= 0:
                    continue

                spread_pct = ((far_price / near_price) - 1) * 100

                near_type = near["contract_type"]
                far_type = far["contract_type"]

                annualised_basis_pct = None
                days_basis = None

                if near_type == "PERPETUAL" and far.get("delivery_dt"):
                    days_basis = days_between(now, far["delivery_dt"])

                elif near.get("delivery_dt") and far.get("delivery_dt"):
                    days_basis = days_between(near["delivery_dt"], far["delivery_dt"])

                if days_basis and days_basis > 0:
                    annualised_basis_pct = spread_pct * 365 / days_basis

                if annualised_basis_pct is None:
                    continue

                if (
                    abs(spread_pct) < MIN_ABS_SPREAD_PCT
                    and abs(annualised_basis_pct) < MIN_ABS_ANNUALISED_BASIS_PCT
                ):
                    continue

                rows.append({
                    "pair_key": pair_key,
                    "near_symbol": near["symbol"],
                    "near_type": near_type,
                    "near_price": near_price,
                    "near_delivery": near.get("delivery_dt"),
                    "far_symbol": far["symbol"],
                    "far_type": far_type,
                    "far_price": far_price,
                    "far_delivery": far.get("delivery_dt"),
                    "spread_pct": spread_pct,
                    "days_basis": days_basis,
                    "annualised_basis_pct": annualised_basis_pct,
                })

    rows = sorted(
        rows,
        key=lambda x: abs(x["annualised_basis_pct"]),
        reverse=True
    )

    return rows


def fmt_dt(dt):
    if dt is None:
        return "PERP"

    return dt.strftime("%Y-%m-%d")


def main():
    metadata = build_symbol_metadata()
    price_lookup = build_price_lookup()
    grouped = group_contracts_by_pair(metadata, price_lookup)
    rows = calculate_spread_rows(grouped)

    print("\nBinance Futures Calendar Spread Scanner")
    print("---------------------------------------")
    print(f"Contracts with metadata: {len(metadata)}")
    print(f"Contracts with prices:   {len(price_lookup)}")
    print(f"Grouped underlyings:     {len(grouped)}")
    print(f"Spread rows found:       {len(rows)}")

    print("\nTop calendar / basis spreads")
    print(
        "Pair        "
        "Near Symbol          "
        "Near Type          "
        "Far Symbol           "
        "Far Type           "
        "Spread %   "
        "Days   "
        "Ann Basis %   "
        "Near Px       "
        "Far Px"
    )

    for row in rows[:30]:
        print(
            f"{row['pair_key']:<11}"
            f"{row['near_symbol']:<21}"
            f"{row['near_type']:<19}"
            f"{row['far_symbol']:<21}"
            f"{row['far_type']:<19}"
            f"{row['spread_pct']:>8.4f}   "
            f"{row['days_basis']:>5.1f}   "
            f"{row['annualised_basis_pct']:>11.2f}   "
            f"{row['near_price']:>10.4f}   "
            f"{row['far_price']:>10.4f}"
        )


if __name__ == "__main__":
    main()