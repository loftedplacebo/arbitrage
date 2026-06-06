# binance_client.py

import requests

from config import BINANCE_FUTURES_BASE_URL


class BinanceFuturesClient:
    def __init__(self):
        self.base_url = BINANCE_FUTURES_BASE_URL

    def _get(self, path, params=None):
        url = f"{self.base_url}{path}"

        response = requests.get(
            url,
            params=params,
            timeout=20
        )
        response.raise_for_status()

        return response.json()

    def get_all_funding_snapshots(self):
        """
        Returns all USD-M futures mark/index/funding snapshots.
        Endpoint: /fapi/v1/premiumIndex
        """
        return self._get("/fapi/v1/premiumIndex")

    def get_24h_tickers(self):
        """
        Returns all USD-M 24h ticker statistics.
        Endpoint: /fapi/v1/ticker/24hr
        """
        return self._get("/fapi/v1/ticker/24hr")

    def get_orderbook_snapshot(self, symbol, limit=100):
        """
        Returns order book depth for a single symbol.
        Endpoint: /fapi/v1/depth
        """
        return self._get(
            "/fapi/v1/depth",
            params={
                "symbol": symbol,
                "limit": limit,
            }
        )