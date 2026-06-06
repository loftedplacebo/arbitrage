from __future__ import annotations

import requests
from datetime import datetime, timezone
from typing import Optional

from core.models import OrderBook, FundingInfo
from core.orderbook import parse_orderbook_levels
from core.symbols import standard_to_exchange_symbol


BITGET_BASE_URL = "https://api.bitget.com"
BITGET_PRODUCT_TYPE = "USDT-FUTURES"


class BitgetMarketAdapter:
    exchange = "bitget"

    def __init__(self):
        self.session = requests.Session()

    def _get(self, path: str, params: Optional[dict] = None):
        response = self.session.get(
            f"{BITGET_BASE_URL}{path}",
            params=params,
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()

        if isinstance(payload, dict) and payload.get("code") not in (None, "00000"):
            raise ValueError(
                f"Bitget API error code={payload.get('code')} msg={payload.get('msg')} path={path}"
            )

        return payload

    def get_futures_orderbook(self, standard_symbol: str, limit: int = 100) -> OrderBook:
        exchange_symbol = standard_to_exchange_symbol(
            standard_symbol,
            exchange="bitget",
            market_type="futures",
        )

        data = self._get(
            "/api/v2/mix/market/merge-depth",
            params={
                "symbol": exchange_symbol,
                "productType": BITGET_PRODUCT_TYPE,
                "limit": str(limit),
            },
        )

        payload = data.get("data") or {}

        return OrderBook(
            exchange="bitget",
            market_type="futures",
            standard_symbol=standard_symbol,
            exchange_symbol=exchange_symbol,
            bids=parse_orderbook_levels(payload.get("bids", []), max_levels=limit),
            asks=parse_orderbook_levels(payload.get("asks", []), max_levels=limit),
            observed_at_utc=datetime.now(timezone.utc),
        )

    def get_funding_info(self, standard_symbol: str) -> FundingInfo:
        exchange_symbol = standard_to_exchange_symbol(
            standard_symbol,
            exchange="bitget",
            market_type="futures",
        )

        # Current funding rate
        rate_payload = self._get(
            "/api/v2/mix/market/current-fund-rate",
            params={
                "symbol": exchange_symbol,
                "productType": BITGET_PRODUCT_TYPE,
            },
        )

        rate_data = rate_payload.get("data") or []
        if isinstance(rate_data, list) and rate_data:
            rate_data = rate_data[0]
        elif not isinstance(rate_data, dict):
            rate_data = {}

        funding_rate_raw = rate_data.get("fundingRate")

        # Next funding time / interval
        time_payload = self._get(
            "/api/v2/mix/market/funding-time",
            params={
                "symbol": exchange_symbol,
                "productType": BITGET_PRODUCT_TYPE,
            },
        )

        time_data = time_payload.get("data") or []
        if isinstance(time_data, list) and time_data:
            time_data = time_data[0]
        elif not isinstance(time_data, dict):
            time_data = {}

        next_funding_ms = (
            time_data.get("nextFundingTime")
            or time_data.get("fundingTime")
            or time_data.get("nextUpdate")
        )

        interval_hours = (
            time_data.get("ratePeriod")
            or time_data.get("fundingRateInterval")
        )

        next_funding_time_utc = None
        if next_funding_ms:
            next_funding_time_utc = datetime.fromtimestamp(
                int(next_funding_ms) / 1000,
                tz=timezone.utc,
            )

        return FundingInfo(
            exchange="bitget",
            standard_symbol=standard_symbol,
            exchange_symbol=exchange_symbol,
            funding_rate=float(funding_rate_raw) if funding_rate_raw is not None else None,
            next_funding_time_utc=next_funding_time_utc,
            funding_interval_hours=int(interval_hours) if interval_hours is not None else None,
            observed_at_utc=datetime.now(timezone.utc),
            stability_score=None,
        )

    def get_futures_usdt_symbols(self) -> set[str]:
        data = self._get(
            "/api/v2/mix/market/contracts",
            params={"productType": BITGET_PRODUCT_TYPE},
        )

        contracts = data.get("data") or []
        symbols: set[str] = set()

        for item in contracts:
            symbol = item.get("symbol")
            quote_coin = item.get("quoteCoin")
            symbol_status = item.get("symbolStatus") or item.get("status")

            # Be permissive on status because Bitget field names can vary.
            status_ok = symbol_status in (None, "normal", "Normal", "listed", "online")

            if symbol and quote_coin == "USDT" and status_ok:
                symbols.add(symbol)

        return symbols

    def get_futures_24h_tickers(self) -> list[dict]:
        data = self._get(
            "/api/v2/mix/market/tickers",
            params={"productType": BITGET_PRODUCT_TYPE},
        )

        tickers = data.get("data") or []
        return tickers if isinstance(tickers, list) else []

    def get_liquidity_ranked_futures_symbols(self, max_symbols: int | None = None) -> list[str]:
        valid_symbols = self.get_futures_usdt_symbols()
        tickers = self.get_futures_24h_tickers()

        ranked = []

        for item in tickers:
            symbol = item.get("symbol")
            if symbol not in valid_symbols:
                continue

            usdt_volume = float(item.get("usdtVolume", 0) or 0)
            quote_volume = float(item.get("quoteVolume", 0) or 0)
            combined_volume = max(usdt_volume, quote_volume)

            ranked.append((symbol, combined_volume))

        ranked = sorted(ranked, key=lambda x: x[1], reverse=True)

        symbols = [symbol for symbol, _ in ranked]

        if max_symbols is not None:
            symbols = symbols[:max_symbols]

        return symbols
    
    def get_fast_futures_tickers(self) -> dict[str, dict]:
        tickers = self.get_futures_24h_tickers()
        output = {}

        for item in tickers:
            symbol = item.get("symbol")
            if not symbol or not symbol.endswith("USDT"):
                continue

            bid = float(item.get("bidPr") or 0)
            ask = float(item.get("askPr") or 0)
            volume_usdt = float(
                item.get("usdtVolume")
                or item.get("quoteVolume")
                or 0
            )

            if bid <= 0 or ask <= 0:
                continue

            output[symbol] = {
                "exchange": "bitget",
                "symbol": symbol,
                "bid": bid,
                "ask": ask,
                "volume_usdt": volume_usdt,
            }

        return output