# inspect_funding.py

import json
import requests


SYMBOL = "SOXSUSDTM"


def main():
    url = "https://api.kucoin.com/api/ua/v1/market/funding-rate"

    response = requests.get(url, params={"symbol": SYMBOL}, timeout=20)
    response.raise_for_status()

    payload = response.json()

    print(json.dumps(payload, indent=4))

    data = payload.get("data", {})
    rate = data.get("nextFundingRate")

    if rate is not None:
        print()
        print(f"{SYMBOL} nextFundingRate raw: {rate}")
        print(f"{SYMBOL} nextFundingRate pct: {float(rate) * 100:.4f}%")


if __name__ == "__main__":
    main()