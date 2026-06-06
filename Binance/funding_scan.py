# funding_scan.py

import requests
from datetime import datetime, timezone


BASE_URL = "https://fapi.binance.com"


def ms_to_utc(ms_value):
    if ms_value is None:
        return None

    try:
        return datetime.fromtimestamp(
            int(ms_value) / 1000,
            tz=timezone.utc
        ).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(ms_value)


def hours_to_funding(ms_value):
    if ms_value is None:
        return None

    try:
        now_ms = datetime.now(timezone.utc).timestamp() * 1000
        return max(0, (int(ms_value) - now_ms) / 1000 / 60 / 60)
    except Exception:
        return None


def get_all_binance_funding():
    url = f"{BASE_URL}/fapi/v1/premiumIndex"

    response = requests.get(url, timeout=20)
    response.raise_for_status()

    return response.json()


def safe_float(value, default=0):
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def main():
    rows = []

    data = get_all_binance_funding()

    for item in data:
        symbol = item.get("symbol")
        funding_rate = safe_float(item.get("lastFundingRate"), 0)
        funding_pct = funding_rate * 100
        abs_funding_pct = abs(funding_pct)

        next_funding_time = item.get("nextFundingTime")

        mark_price = safe_float(item.get("markPrice"))
        index_price = safe_float(item.get("indexPrice"))

        premium_pct = None
        if mark_price and index_price:
            premium_pct = ((mark_price - index_price) / index_price) * 100

        rows.append({
            "symbol": symbol,
            "funding_pct": funding_pct,
            "abs_funding_pct": abs_funding_pct,
            "next_funding_time": next_funding_time,
            "hours_to_funding": hours_to_funding(next_funding_time),
            "mark_price": mark_price,
            "index_price": index_price,
            "premium_pct": premium_pct,
        })

    rows = sorted(
        rows,
        key=lambda x: x["abs_funding_pct"],
        reverse=True
    )

    print("\nTop 20 Binance funding rates by absolute funding")
    print(
        "Symbol       "
        "Funding %   "
        "Abs Fund %   "
        "Hours To Funding   "
        "Next Funding UTC        "
        "Premium %   "
        "Mark        "
        "Index"
    )

    for row in rows[:20]:
        print(
            f"{row['symbol']:<12}"
            f"{row['funding_pct']:>9.4f}   "
            f"{row['abs_funding_pct']:>10.4f}   "
            f"{(row['hours_to_funding'] or 0):>16.2f}   "
            f"{ms_to_utc(row['next_funding_time']):<23}"
            f"{(row['premium_pct'] or 0):>9.4f}   "
            f"{row['mark_price']:>10.6f}   "
            f"{row['index_price']:>10.6f}"
        )


if __name__ == "__main__":
    main()