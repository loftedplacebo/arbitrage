# inspect_funding.py

import requests
from datetime import datetime, timezone


BASE_URL = "https://fapi.binance.com"
SYMBOL = "ERAUSDT"


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


def minutes_to_funding(ms_value):
    if ms_value is None:
        return None

    try:
        now_ms = datetime.now(timezone.utc).timestamp() * 1000
        return max(0, (int(ms_value) - now_ms) / 1000 / 60)
    except Exception:
        return None


def get_binance_funding_snapshot(symbol):
    url = f"{BASE_URL}/fapi/v1/premiumIndex"

    response = requests.get(
        url,
        params={"symbol": symbol},
        timeout=20
    )
    response.raise_for_status()

    return response.json()


def main():
    data = get_binance_funding_snapshot(SYMBOL)

    last_funding_rate = float(data.get("lastFundingRate", 0))
    next_funding_time = data.get("nextFundingTime")

    print("\nBinance USD-M Futures Funding Check")
    print("----------------------------------")
    print(f"Symbol:              {data.get('symbol')}")
    print(f"Mark price:          {data.get('markPrice')}")
    print(f"Index price:         {data.get('indexPrice')}")
    print(f"Estimated settle:    {data.get('estimatedSettlePrice')}")
    print(f"Last funding raw:    {data.get('lastFundingRate')}")
    print(f"Last funding %:      {last_funding_rate * 100:.4f}%")
    print(f"Next funding raw:    {next_funding_time}")
    print(f"Next funding UTC:    {ms_to_utc(next_funding_time)}")
    print(f"Hours to funding:    {(minutes_to_funding(next_funding_time) or 0) / 60:.2f}")
    print(f"Interest rate:       {data.get('interestRate')}")
    print(f"Time:                {ms_to_utc(data.get('time'))}")


if __name__ == "__main__":
    main()