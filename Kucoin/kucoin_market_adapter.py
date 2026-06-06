from __future__ import annotations

import requests
from datetime import datetime, timezone
from typing import Optional

from core.models import OrderBook, FundingInfo
from core.orderbook import parse_orderbook_levels
from core.symbols import normalise_symbol, standard_to_exchange_symbol


KUCOIN_FUTURES_BASE_URL = "https://api-futures.kucoin.com"
KUCOIN_FUNDING_BASE_URL = "https://api.kucoin.com"


class KucoinMarketAdapter:
    exchange = "kucoin"

    def __init__(self):
        self.session = requests.Session()

    def _get_futures(self, path: str, params: Optional[dict] = None):
        response = self.session.get(
            f"{KUCOIN_FUTURES_BASE_URL}{path}",
            params=params,
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()

        if payload.get("code") != "200000":
            raise RuntimeError(f"KuCoin futures API error: {payload}")

        return payload.get("data")

    def _get_spot_api(self, path: str, params: Optional[dict] = None):
        response = self.session.get(
            f"{KUCOIN_FUNDING_BASE_URL}{path}",
            params=params,
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()

        if payload.get("code") != "200000":
            raise RuntimeError(f"KuCoin API error: {payload}")

        return payload.get("data")

    def get_futures_orderbook(self, standard_symbol: str, limit: int = 100) -> OrderBook:
        exchange_symbol = standard_to_exchange_symbol(
            standard_symbol,
            exchange="kucoin",
            market_type="futures",
        )

        data = self._get_futures(
            "/api/v1/level2/snapshot",
            params={"symbol": exchange_symbol},
        )

        return OrderBook(
            exchange="kucoin",
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
            exchange="kucoin",
            market_type="futures",
        )

        data = self._get_spot_api(
            "/api/ua/v1/market/funding-rate",
            params={"symbol": exchange_symbol},
        )

        funding_rate_raw = data.get("nextFundingRate")
        next_funding_ms = data.get("fundingTime")

        next_funding_time_utc = None
        if next_funding_ms:
            next_funding_time_utc = datetime.fromtimestamp(
                int(next_funding_ms) / 1000,
                tz=timezone.utc,
            )

        return FundingInfo(
            exchange="kucoin",
            standard_symbol=standard_symbol,
            exchange_symbol=exchange_symbol,
            funding_rate=float(funding_rate_raw) if funding_rate_raw is not None else None,
            next_funding_time_utc=next_funding_time_utc,
            funding_interval_hours=8,
            observed_at_utc=datetime.now(timezone.utc),
            stability_score=None,
        )

    def get_active_contracts(self) -> list[dict]:
        data = self._get_futures("/api/v1/contracts/active")
        return data if isinstance(data, list) else []

    def get_futures_usdt_symbols(self) -> set[str]:
        contracts = self.get_active_contracts()
        symbols: set[str] = set()

        for contract in contracts:
            exchange_symbol = contract.get("symbol")
            quote_currency = contract.get("quoteCurrency")
            status = contract.get("status")

            if (
                exchange_symbol
                and str(exchange_symbol).endswith("USDTM")
                and quote_currency == "USDT"
                and status == "Open"
            ):
                symbols.add(normalise_symbol(exchange_symbol))

        return symbols

    def get_liquidity_ranked_futures_symbols(self, max_symbols: int | None = None) -> list[str]:
        contracts = self.get_active_contracts()

        ranked = []

        for contract in contracts:
            exchange_symbol = contract.get("symbol")
            quote_currency = contract.get("quoteCurrency")
            status = contract.get("status")

            if not (
                exchange_symbol
                and str(exchange_symbol).endswith("USDTM")
                and quote_currency == "USDT"
                and status == "Open"
            ):
                continue

            standard_symbol = normalise_symbol(exchange_symbol)

            turnover_24h = float(contract.get("turnoverOf24h") or 0)
            ranked.append((standard_symbol, turnover_24h))

        ranked = sorted(ranked, key=lambda x: x[1], reverse=True)
        symbols = [symbol for symbol, _ in ranked]

        if max_symbols is not None:
            symbols = symbols[:max_symbols]

        return symbols
    
    def get_futures_all_tickers(self) -> list[dict]:
        data = self._get_futures("/api/v1/allTickers")

        if isinstance(data, dict):
            tickers = data.get("ticker") or data.get("tickers") or data.get("data") or []
            return tickers if isinstance(tickers, list) else []

        return data if isinstance(data, list) else []
    
    def get_fast_futures_tickers(self) -> dict[str, dict]:
        """
        Fast KuCoin futures ticker data.

        Uses:
        - /api/v1/allTickers for best bid/ask
        - /api/v1/contracts/active for turnoverOf24h

        KuCoin allTickers does not include 24h turnover, so we enrich it from
        the contracts endpoint.
        """
        tickers = self.get_futures_all_tickers()
        contracts = self.get_active_contracts()

        turnover_lookup = {}

        for contract in contracts:
            exchange_symbol = contract.get("symbol")
            if not exchange_symbol:
                continue

            standard_symbol = normalise_symbol(exchange_symbol)
            turnover_lookup[standard_symbol] = float(contract.get("turnoverOf24h") or 0)

        output = {}

        for item in tickers:
            exchange_symbol = item.get("symbol")
            if not exchange_symbol:
                continue

            symbol = normalise_symbol(exchange_symbol)

            if not symbol.endswith("USDT"):
                continue

            bid = float(
                item.get("bestBidPrice")
                or item.get("bidPrice")
                or item.get("buy")
                or 0
            )

            ask = float(
                item.get("bestAskPrice")
                or item.get("askPrice")
                or item.get("sell")
                or 0
            )

            volume_usdt = turnover_lookup.get(symbol, 0)

            if bid <= 0 or ask <= 0:
                continue

            output[symbol] = {
                "exchange": "kucoin",
                "symbol": symbol,
                "bid": bid,
                "ask": ask,
                "volume_usdt": volume_usdt,
            }

        return output