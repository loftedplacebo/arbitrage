# kucoin_client.py

import requests
from config import KUCOIN_FUTURES_BASE_URL


class KuCoinFuturesClient:
    def __init__(self):
        self.base_url = KUCOIN_FUTURES_BASE_URL

    def _get(self, path, params=None):
        url = f"{self.base_url}{path}"

        response = requests.get(url, params=params, timeout=8)
        response.raise_for_status()

        payload = response.json()

        if payload.get("code") != "200000":
            raise RuntimeError(f"KuCoin API error: {payload}")

        return payload.get("data")

    def get_active_contracts(self):
        return self._get("/api/v1/contracts/active")

    def get_orderbook_snapshot(self, symbol):
        return self._get("/api/v1/level2/snapshot", params={"symbol": symbol})
    
    def get_current_funding_rate(self, symbol):
        url = "https://api.kucoin.com/api/ua/v1/market/funding-rate"

        response = requests.get(
            url,
            params={"symbol": symbol},
            timeout=20
        )
        response.raise_for_status()

        payload = response.json()

        if payload.get("code") != "200000":
            raise RuntimeError(f"KuCoin funding API error for {symbol}: {payload}")

        return payload.get("data")
    
    def get_current_funding_rate(self, symbol):
        url = "https://api.kucoin.com/api/ua/v1/market/funding-rate"

        response = requests.get(
            url,
            params={"symbol": symbol},
            timeout=20
        )
        response.raise_for_status()

        payload = response.json()

        if payload.get("code") != "200000":
            raise RuntimeError(f"KuCoin funding API error for {symbol}: {payload}")

        return payload.get("data")