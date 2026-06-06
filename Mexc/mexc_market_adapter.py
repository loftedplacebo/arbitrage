from __future__ import annotations

import requests
from datetime import datetime, timezone
from typing import Optional

from core.models import OrderBook, FundingInfo
from core.orderbook import parse_orderbook_levels
from core.symbols import standard_to_exchange_symbol, normalise_symbol


MEXC_BASE_URL = "https://contract.mexc.com"


class MexcMarketAdapter:
    exchange = "mexc"

    def __init__(self):
        self.session = requests.Session()

    def _get(self, path: str, params: Optional[dict] = None):
        response = self.session.get(
            f"{MEXC_BASE_URL}{path}",
            params=params,
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()

        if isinstance(payload, dict) and payload.get("success") is False:
            raise ValueError(
                f"MEXC API error code={payload.get('code')} msg={payload.get('message')} path={path}"
            )

        return payload

    def get_futures_orderbook(self, standard_symbol: str, limit: int = 100) -> OrderBook:
        exchange_symbol = standard_to_exchange_symbol(
            standard_symbol,
            exchange="mexc",
            market_type="futures",
        )

        data = self._get(
            f"/api/v1/contract/depth/{exchange_symbol}",
            params={"limit": limit},
        )

        payload = data.get("data") or {}

        return OrderBook(
            exchange="mexc",
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
            exchange="mexc",
            market_type="futures",
        )

        data = self._get(
            f"/api/v1/contract/funding_rate/{exchange_symbol}",
        )

        payload = data.get("data") or {}

        funding_rate_raw = payload.get("fundingRate")
        next_settle_ms = payload.get("nextSettleTime")
        collect_cycle = payload.get("collectCycle")

        next_funding_time_utc = None
        if next_settle_ms:
            next_funding_time_utc = datetime.fromtimestamp(
                int(next_settle_ms) / 1000,
                tz=timezone.utc,
            )

        return FundingInfo(
            exchange="mexc",
            standard_symbol=standard_symbol,
            exchange_symbol=exchange_symbol,
            funding_rate=float(funding_rate_raw) if funding_rate_raw is not None else None,
            next_funding_time_utc=next_funding_time_utc,
            funding_interval_hours=int(collect_cycle) if collect_cycle is not None else None,
            observed_at_utc=datetime.now(timezone.utc),
            stability_score=None,
        )

    def get_futures_usdt_symbols(self) -> set[str]:
        data = self._get("/api/v1/contract/detail")

        contracts = data.get("data") or []
        symbols: set[str] = set()

        if isinstance(contracts, dict):
            contracts = list(contracts.values())

        for item in contracts:
            if not isinstance(item, dict):
                continue

            exchange_symbol = item.get("symbol")
            quote_coin = item.get("quoteCoin")
            settle_coin = item.get("settleCoin")
            state = item.get("state")

            # MEXC state 0 is commonly normal/enabled.
            state_ok = state in (None, 0, "0")

            if (
                exchange_symbol
                and state_ok
                and (quote_coin == "USDT" or settle_coin == "USDT" or str(exchange_symbol).endswith("_USDT"))
            ):
                symbols.add(normalise_symbol(exchange_symbol))

        return symbols

    def get_futures_24h_tickers(self) -> list[dict]:
        data = self._get("/api/v1/contract/ticker")
        tickers = data.get("data") or []
        return tickers if isinstance(tickers, list) else []

    def get_liquidity_ranked_futures_symbols(self, max_symbols: int | None = None) -> list[str]:
        valid_symbols = self.get_futures_usdt_symbols()
        tickers = self.get_futures_24h_tickers()

        ranked = []

        for item in tickers:
            exchange_symbol = item.get("symbol")
            if not exchange_symbol:
                continue

            standard_symbol = normalise_symbol(exchange_symbol)

            if standard_symbol not in valid_symbols:
                continue

            # MEXC ticker fields can vary. Try the common volume fields.
            amount24 = float(
                item.get("amount24")
                or item.get("amount24h")
                or item.get("quoteVolume")
                or item.get("volume24")
                or 0
            )

            ranked.append((standard_symbol, amount24))

        ranked = sorted(ranked, key=lambda x: x[1], reverse=True)
        symbols = [symbol for symbol, _ in ranked]

        if max_symbols is not None:
            symbols = symbols[:max_symbols]

        return symbols
    
    def get_fast_futures_tickers(self) -> dict[str, dict]:
            tickers = self.get_futures_24h_tickers()
            output = {}

            for item in tickers:
                exchange_symbol = item.get("symbol")
                if not exchange_symbol:
                    continue

                symbol = normalise_symbol(exchange_symbol)

                bid = float(item.get("bid1") or item.get("bidPrice") or 0)
                ask = float(item.get("ask1") or item.get("askPrice") or 0)
                volume_usdt = float(
                    item.get("amount24")
                    or item.get("amount24h")
                    or item.get("quoteVolume")
                    or item.get("volume24")
                    or 0
                )

                if bid <= 0 or ask <= 0:
                    continue

                output[symbol] = {
                    "exchange": "mexc",
                    "symbol": symbol,
                    "bid": bid,
                    "ask": ask,
                    "volume_usdt": volume_usdt,
                }

            return output    