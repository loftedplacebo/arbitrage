from __future__ import annotations

import requests
from datetime import datetime, timezone
from typing import Optional

from core.models import OrderBook, FundingInfo
from core.orderbook import parse_orderbook_levels
from core.symbols import standard_to_exchange_symbol


BINANCE_SPOT_BASE_URL = "https://api.binance.com"
BINANCE_FUTURES_BASE_URL = "https://fapi.binance.com"


class BinanceMarketAdapter:
    exchange = "binance"

    def __init__(self):
        self.session = requests.Session()

    def _get(self, base_url: str, path: str, params: Optional[dict] = None):
        response = self.session.get(
            f"{base_url}{path}",
            params=params,
            timeout=20,
        )
        response.raise_for_status()
        return response.json()

    def get_spot_orderbook(self, standard_symbol: str, limit: int = 100) -> OrderBook:
        exchange_symbol = standard_to_exchange_symbol(
            standard_symbol,
            exchange="binance",
            market_type="spot",
        )

        data = self._get(
            BINANCE_SPOT_BASE_URL,
            "/api/v3/depth",
            params={
                "symbol": exchange_symbol,
                "limit": limit,
            },
        )

        return OrderBook(
            exchange="binance",
            market_type="spot",
            standard_symbol=standard_symbol,
            exchange_symbol=exchange_symbol,
            bids=parse_orderbook_levels(data.get("bids", []), max_levels=limit),
            asks=parse_orderbook_levels(data.get("asks", []), max_levels=limit),
            observed_at_utc=datetime.now(timezone.utc),
        )

    def get_futures_orderbook(self, standard_symbol: str, limit: int = 100) -> OrderBook:
        exchange_symbol = standard_to_exchange_symbol(
            standard_symbol,
            exchange="binance",
            market_type="futures",
        )

        data = self._get(
            BINANCE_FUTURES_BASE_URL,
            "/fapi/v1/depth",
            params={
                "symbol": exchange_symbol,
                "limit": limit,
            },
        )

        return OrderBook(
            exchange="binance",
            market_type="futures",
            standard_symbol=standard_symbol,
            exchange_symbol=exchange_symbol,
            bids=parse_orderbook_levels(data.get("bids", []), max_levels=limit),
            asks=parse_orderbook_levels(data.get("asks", []), max_levels=limit),
            observed_at_utc=datetime.now(timezone.utc),
        )

    def get_funding_info(self, standard_symbol: str) -> FundingInfo:
        exchange_symbol = standard_to_exchange_symbol(
            standard_symbol,
            exchange="binance",
            market_type="futures",
        )

        data = self._get(
            BINANCE_FUTURES_BASE_URL,
            "/fapi/v1/premiumIndex",
            params={"symbol": exchange_symbol},
        )

        funding_rate = data.get("lastFundingRate")
        next_funding_ms = data.get("nextFundingTime")

        next_funding_time_utc = None
        if next_funding_ms:
            next_funding_time_utc = datetime.fromtimestamp(
                int(next_funding_ms) / 1000,
                tz=timezone.utc,
            )

        return FundingInfo(
            exchange="binance",
            standard_symbol=standard_symbol,
            exchange_symbol=exchange_symbol,
            funding_rate=float(funding_rate) if funding_rate is not None else None,
            next_funding_time_utc=next_funding_time_utc,
            funding_interval_hours=8,
            observed_at_utc=datetime.now(timezone.utc),
            stability_score=None,
        )
    

    def get_spot_usdt_symbols(self) -> set[str]:
        data = self._get(
            BINANCE_SPOT_BASE_URL,
            "/api/v3/exchangeInfo",
        )

        symbols = set()

        for item in data.get("symbols", []):
            symbol = item.get("symbol")
            status = item.get("status")
            quote_asset = item.get("quoteAsset")
            is_spot_allowed = item.get("isSpotTradingAllowed")

            if (
                symbol
                and status == "TRADING"
                and quote_asset == "USDT"
                and is_spot_allowed
            ):
                symbols.add(symbol)

        return symbols

    def get_futures_usdt_symbols(self) -> set[str]:
        data = self._get(
            BINANCE_FUTURES_BASE_URL,
            "/fapi/v1/exchangeInfo",
        )

        symbols = set()

        for item in data.get("symbols", []):
            symbol = item.get("symbol")
            status = item.get("status")
            quote_asset = item.get("quoteAsset")
            contract_type = item.get("contractType")

            if (
                symbol
                and status == "TRADING"
                and quote_asset == "USDT"
                and contract_type == "PERPETUAL"
            ):
                symbols.add(symbol)

        return symbols

    def get_common_spot_futures_usdt_symbols(self) -> list[str]:
        spot_symbols = self.get_spot_usdt_symbols()
        futures_symbols = self.get_futures_usdt_symbols()

        common = sorted(spot_symbols.intersection(futures_symbols))
        return common
    
    def get_spot_24h_tickers(self) -> list[dict]:
        return self._get(
            BINANCE_SPOT_BASE_URL,
            "/api/v3/ticker/24hr",
        )

    def get_futures_24h_tickers(self) -> list[dict]:
        return self._get(
            BINANCE_FUTURES_BASE_URL,
            "/fapi/v1/ticker/24hr",
        )

    def get_liquidity_ranked_common_symbols(self, max_symbols: int | None = None) -> list[str]:
        """
        Return common spot/futures USDT symbols ranked by combined spot + futures quote volume.
        """
        common_symbols = set(self.get_common_spot_futures_usdt_symbols())

        spot_tickers = self.get_spot_24h_tickers()
        futures_tickers = self.get_futures_24h_tickers()

        spot_volume = {
            item.get("symbol"): float(item.get("quoteVolume", 0) or 0)
            for item in spot_tickers
            if item.get("symbol") in common_symbols
        }

        futures_volume = {
            item.get("symbol"): float(item.get("quoteVolume", 0) or 0)
            for item in futures_tickers
            if item.get("symbol") in common_symbols
        }

        ranked = []

        for symbol in common_symbols:
            combined_volume = spot_volume.get(symbol, 0) + futures_volume.get(symbol, 0)
            ranked.append((symbol, combined_volume))

        ranked = sorted(ranked, key=lambda x: x[1], reverse=True)

        symbols = [symbol for symbol, _ in ranked]

        if max_symbols is not None:
            symbols = symbols[:max_symbols]

        return symbols
    
    def get_futures_24h_tickers(self) -> list[dict]:
        return self._get(
            BINANCE_FUTURES_BASE_URL,
            "/fapi/v1/ticker/24hr",
        )

    def get_futures_book_tickers(self) -> list[dict]:
        return self._get(
            BINANCE_FUTURES_BASE_URL,
            "/fapi/v1/ticker/bookTicker",
        )

    def get_futures_24h_tickers(self) -> list[dict]:
        return self._get(
            BINANCE_FUTURES_BASE_URL,
            "/fapi/v1/ticker/24hr",
        )

    def get_fast_futures_tickers(self) -> dict[str, dict]:
        """
        Fast futures ticker data using:
        - bookTicker for bid/ask
        - 24hr ticker for quote volume
        """
        book_tickers = self.get_futures_book_tickers()
        volume_tickers = self.get_futures_24h_tickers()

        volume_lookup = {
            item.get("symbol"): float(item.get("quoteVolume") or 0)
            for item in volume_tickers
            if item.get("symbol")
        }

        output = {}

        for item in book_tickers:
            symbol = item.get("symbol")
            if not symbol or not symbol.endswith("USDT"):
                continue

            bid = float(item.get("bidPrice") or 0)
            ask = float(item.get("askPrice") or 0)
            volume_usdt = volume_lookup.get(symbol, 0)

            if bid <= 0 or ask <= 0:
                continue

            output[symbol] = {
                "exchange": "binance",
                "symbol": symbol,
                "bid": bid,
                "ask": ask,
                "volume_usdt": volume_usdt,
            }

        return output