from __future__ import annotations

import math
import requests
from datetime import datetime, timedelta, timezone
from typing import Optional

from core.models import FundingInfo, OrderBook
from core.orderbook import parse_orderbook_levels
from core.symbols import standard_to_exchange_symbol


HYPERLIQUID_INFO_URL = "https://api.hyperliquid.xyz/info"


class HyperliquidMarketAdapter:
    exchange = "hyperliquid"

    def __init__(self):
        self.session = requests.Session()
        self._asset_context_by_coin: dict[str, dict] = {}

    def _post(self, payload: dict):
        response = self.session.post(
            HYPERLIQUID_INFO_URL,
            json=payload,
            timeout=20,
        )
        response.raise_for_status()
        return response.json()

    @staticmethod
    def _standard_symbol_from_coin(coin: str) -> str:
        return f"{coin.upper()}USDT"

    @staticmethod
    def _is_supported_perp_coin(coin: str) -> bool:
        return bool(coin) and coin.isalnum() and ":" not in coin and not coin.startswith("@")

    @staticmethod
    def _next_hour_utc(now: datetime) -> datetime:
        return (now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))

    def get_meta_and_asset_contexts(self) -> tuple[dict, list[dict]]:
        payload = self._post({"type": "metaAndAssetCtxs"})
        if not isinstance(payload, list) or len(payload) < 2:
            raise ValueError(f"Unexpected Hyperliquid metaAndAssetCtxs response: {payload!r}")

        meta = payload[0] if isinstance(payload[0], dict) else {}
        contexts = payload[1] if isinstance(payload[1], list) else []
        universe = meta.get("universe") or []

        self._asset_context_by_coin = {}
        for asset, context in zip(universe, contexts):
            coin = asset.get("name") if isinstance(asset, dict) else None
            if coin and isinstance(context, dict):
                self._asset_context_by_coin[str(coin)] = context

        return meta, contexts

    def get_all_mids(self) -> dict[str, str]:
        mids = self._post({"type": "allMids"})
        return mids if isinstance(mids, dict) else {}

    def get_futures_orderbook(self, standard_symbol: str, limit: int = 100) -> OrderBook:
        exchange_symbol = standard_to_exchange_symbol(
            standard_symbol,
            exchange="hyperliquid",
            market_type="futures",
        )

        data = self._post({"type": "l2Book", "coin": exchange_symbol})
        levels = data.get("levels") if isinstance(data, dict) else None
        bids = levels[0] if isinstance(levels, list) and len(levels) > 0 else []
        asks = levels[1] if isinstance(levels, list) and len(levels) > 1 else []
        max_levels = min(limit, 20)

        return OrderBook(
            exchange="hyperliquid",
            market_type="futures",
            standard_symbol=standard_symbol,
            exchange_symbol=exchange_symbol,
            bids=parse_orderbook_levels(bids, max_levels=max_levels),
            asks=parse_orderbook_levels(asks, max_levels=max_levels),
            observed_at_utc=datetime.now(timezone.utc),
        )

    def get_funding_info(self, standard_symbol: str) -> FundingInfo:
        exchange_symbol = standard_to_exchange_symbol(
            standard_symbol,
            exchange="hyperliquid",
            market_type="futures",
        )

        if not self._asset_context_by_coin:
            self.get_meta_and_asset_contexts()

        context = self._asset_context_by_coin.get(exchange_symbol, {})
        funding_rate_raw = context.get("funding") if isinstance(context, dict) else None
        now = datetime.now(timezone.utc)

        return FundingInfo(
            exchange="hyperliquid",
            standard_symbol=standard_symbol,
            exchange_symbol=exchange_symbol,
            funding_rate=float(funding_rate_raw) if funding_rate_raw not in (None, "") else None,
            next_funding_time_utc=self._next_hour_utc(now),
            funding_interval_hours=1,
            observed_at_utc=now,
            stability_score=None,
        )

    def get_futures_usdt_symbols(self) -> set[str]:
        meta, contexts = self.get_meta_and_asset_contexts()
        universe = meta.get("universe") or []
        symbols = set()

        for asset, context in zip(universe, contexts):
            coin = str(asset.get("name") or "") if isinstance(asset, dict) else ""
            if not self._is_supported_perp_coin(coin):
                continue

            if isinstance(asset, dict) and asset.get("isDelisted") is True:
                continue

            if not isinstance(context, dict):
                continue

            try:
                price = float(context.get("midPx") or 0)
                volume_usdt = float(context.get("dayNtlVlm") or 0)
                open_interest = float(context.get("openInterest") or 0)
            except (TypeError, ValueError):
                continue

            if (
                math.isfinite(price)
                and price > 0
                and volume_usdt > 0
                and open_interest > 0
            ):
                symbols.add(self._standard_symbol_from_coin(coin))
        return symbols

    def get_fast_futures_tickers(self) -> dict[str, dict]:
        """
        Hyperliquid's public all-mids endpoint is broad and cheap, but does not
        expose bid/ask. These rows are retained for compatibility, but the fast
        scanner filters them out of tradeable candidate discovery.
        """
        meta, contexts = self.get_meta_and_asset_contexts()
        universe = meta.get("universe") or []
        output = {}

        for asset, context in zip(universe, contexts):
            coin = str(asset.get("name") or "") if isinstance(asset, dict) else ""
            if not self._is_supported_perp_coin(coin):
                continue

            if isinstance(asset, dict) and asset.get("isDelisted") is True:
                continue

            if not isinstance(context, dict):
                continue

            try:
                mid = float(context.get("midPx") or 0)
                volume_usdt = float(context.get("dayNtlVlm") or 0)
                open_interest = float(context.get("openInterest") or 0)
            except (TypeError, ValueError):
                continue

            if not math.isfinite(mid) or mid <= 0 or volume_usdt <= 0 or open_interest <= 0:
                continue

            symbol = self._standard_symbol_from_coin(coin)

            output[symbol] = {
                "exchange": "hyperliquid",
                "symbol": symbol,
                "bid": mid,
                "ask": mid,
                "volume_usdt": volume_usdt,
                "price_source": "rest_mid",
            }

        return output
