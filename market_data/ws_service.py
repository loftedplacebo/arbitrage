from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Iterable

from core.symbols import standard_to_exchange_symbol
from market_data.cache import MarketDataCache
from market_data.ws_parsers import (
    parse_binance_book_ticker,
    parse_binance_depth,
    parse_binance_mark_price_updates,
    parse_bitget_depth,
    parse_bitget_tickers,
    parse_hyperliquid_active_asset_ctx,
    parse_hyperliquid_all_mids,
    parse_hyperliquid_bbo,
    parse_hyperliquid_l2_book,
    parse_mexc_depth,
    parse_mexc_funding,
    parse_mexc_tickers,
)

try:
    import websockets
except ImportError:  # pragma: no cover - exercised only when dependency missing
    websockets = None


BINANCE_BOOK_TICKER_URL = "wss://fstream.binance.com/public/ws/!bookTicker"
BINANCE_MARK_PRICE_URL = "wss://fstream.binance.com/market/ws/!markPrice@arr@1s"
BINANCE_PUBLIC_COMBINED_BASE_URL = "wss://fstream.binance.com/public/stream?streams="
BITGET_PUBLIC_URL = "wss://ws.bitget.com/v2/ws/public"
MEXC_PUBLIC_URL = "wss://contract.mexc.com/edge"
HYPERLIQUID_PUBLIC_URL = "wss://api.hyperliquid.xyz/ws"


@dataclass(frozen=True)
class WebsocketRuntimeConfig:
    enabled_exchanges: set[str] = field(
        default_factory=lambda: {"binance", "bitget", "mexc", "hyperliquid"}
    )
    ticker_symbol_limit: int | None = None
    ticker_batch_size: int = 100
    depth_batch_size: int = 80
    depth_target_ttl_seconds: float = 120.0
    depth_reconnect_seconds: float = 10.0
    reconnect_delay_seconds: float = 5.0
    ping_interval_seconds: float = 15.0


class WebsocketMarketDataService:
    """
    Background websocket collector for scanner market data.

    This service is deliberately optional. The scanner should continue to fall
    back to REST whenever the cache is cold, stale, or missing a venue.
    """

    def __init__(
        self,
        *,
        adapters: dict,
        cache: MarketDataCache,
        config: WebsocketRuntimeConfig | None = None,
    ):
        self.adapters = adapters
        self.cache = cache
        self.config = config or WebsocketRuntimeConfig()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None
        self._started = threading.Event()

    def start(self) -> None:
        if websockets is None:
            raise RuntimeError("websockets is not installed. Run: pip install -r requirements.txt")
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run_thread, name="ws-market-data", daemon=True)
        self._thread.start()
        self._started.wait(timeout=10)

    def stop(self) -> None:
        if self._loop and self._stop_event:
            self._loop.call_soon_threadsafe(self._stop_event.set)
        if self._thread:
            self._thread.join(timeout=10)

    def set_depth_targets(self, candidates: Iterable[dict], *, max_candidates: int) -> None:
        from market_data.scanner_integration import build_depth_targets_from_candidates

        targets = build_depth_targets_from_candidates(
            candidates,
            max_candidates=max_candidates,
        )
        self.cache.set_depth_targets(
            targets,
            ttl_seconds=self.config.depth_target_ttl_seconds,
        )

    def _run_thread(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._stop_event = asyncio.Event()
        self._started.set()
        self._loop.run_until_complete(self._run())

    async def _run(self) -> None:
        assert self._stop_event is not None
        tasks = []
        enabled = self.config.enabled_exchanges

        if "binance" in enabled:
            tasks.extend([
                asyncio.create_task(self._binance_book_ticker_loop()),
                asyncio.create_task(self._binance_mark_price_loop()),
                asyncio.create_task(self._binance_depth_loop()),
            ])
        if "bitget" in enabled and "bitget" in self.adapters:
            symbols = self._limited_symbols("bitget")
            tasks.extend([
                asyncio.create_task(self._bitget_ticker_loop(symbols)),
                asyncio.create_task(self._bitget_depth_loop()),
            ])
        if "mexc" in enabled and "mexc" in self.adapters:
            symbols = self._limited_symbols("mexc")
            tasks.extend([
                asyncio.create_task(self._mexc_ticker_loop(symbols)),
                asyncio.create_task(self._mexc_depth_loop()),
            ])
        if "hyperliquid" in enabled:
            tasks.extend([
                asyncio.create_task(self._hyperliquid_ticker_loop()),
                asyncio.create_task(self._hyperliquid_depth_loop()),
            ])

        await self._stop_event.wait()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def _limited_symbols(self, exchange: str) -> list[str]:
        adapter = self.adapters[exchange]
        symbols = sorted(adapter.get_futures_usdt_symbols())
        if self.config.ticker_symbol_limit is not None:
            symbols = symbols[: self.config.ticker_symbol_limit]
        return symbols

    async def _json_stream_loop(
        self,
        *,
        name: str,
        url: str,
        on_message: Callable[[dict], None],
        subscribe_messages: list[dict] | None = None,
        ping_message: dict | None = None,
    ) -> None:
        assert self._stop_event is not None
        while not self._stop_event.is_set():
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
                    for message in subscribe_messages or []:
                        await ws.send(json.dumps(message))

                    last_ping = asyncio.get_running_loop().time()
                    while not self._stop_event.is_set():
                        if ping_message and (
                            asyncio.get_running_loop().time() - last_ping
                        ) >= self.config.ping_interval_seconds:
                            await ws.send(json.dumps(ping_message))
                            last_ping = asyncio.get_running_loop().time()

                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=5)
                        except asyncio.TimeoutError:
                            continue
                        payload = json.loads(raw)
                        on_message(payload.get("data") if _combined_payload(payload) else payload)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"[ws:{name}] disconnected: {exc}")
                await asyncio.sleep(self.config.reconnect_delay_seconds)

    async def _binance_book_ticker_loop(self) -> None:
        def on_message(payload: dict) -> None:
            ticker = parse_binance_book_ticker(payload)
            if ticker:
                self.cache.update_ticker(ticker)

        await self._json_stream_loop(
            name="binance_book_ticker",
            url=BINANCE_BOOK_TICKER_URL,
            on_message=on_message,
        )

    async def _binance_mark_price_loop(self) -> None:
        def on_message(payload) -> None:
            for symbol, funding_rate, next_funding, observed_at in parse_binance_mark_price_updates(payload):
                self.cache.update_funding(
                    exchange="binance",
                    symbol=symbol,
                    funding_rate=funding_rate,
                    next_funding_time_utc=next_funding,
                    observed_at_utc=observed_at,
                )

        await self._json_stream_loop(
            name="binance_mark_price",
            url=BINANCE_MARK_PRICE_URL,
            on_message=on_message,
        )

    async def _binance_depth_loop(self) -> None:
        while True:
            targets = [
                target.symbol.lower()
                for target in self.cache.get_depth_targets("binance")
            ][: self.config.depth_batch_size]
            if not targets:
                await asyncio.sleep(1)
                continue

            streams = "/".join(f"{symbol}@depth20@500ms" for symbol in targets)
            url = f"{BINANCE_PUBLIC_COMBINED_BASE_URL}{streams}"

            def on_message(payload: dict) -> None:
                orderbook = parse_binance_depth(payload)
                if orderbook:
                    self.cache.update_orderbook(orderbook)

            await self._run_depth_connection(
                name="binance_depth",
                url=url,
                on_message=on_message,
            )

    async def _bitget_ticker_loop(self, symbols: list[str]) -> None:
        subscribe_messages = [
            {
                "op": "subscribe",
                "args": [
                    {"instType": "USDT-FUTURES", "channel": "ticker", "instId": symbol}
                    for symbol in batch
                ],
            }
            for batch in _chunks(symbols, self.config.ticker_batch_size)
        ]

        def on_message(payload: dict) -> None:
            for ticker in parse_bitget_tickers(payload):
                self.cache.update_ticker(ticker)

        await self._json_stream_loop(
            name="bitget_ticker",
            url=BITGET_PUBLIC_URL,
            subscribe_messages=subscribe_messages,
            on_message=on_message,
        )

    async def _bitget_depth_loop(self) -> None:
        while True:
            symbols = [
                target.symbol
                for target in self.cache.get_depth_targets("bitget")
            ][: self.config.depth_batch_size]
            if not symbols:
                await asyncio.sleep(1)
                continue

            subscribe_messages = [
                {
                    "op": "subscribe",
                    "args": [
                        {"instType": "USDT-FUTURES", "channel": "books15", "instId": symbol}
                        for symbol in symbols
                    ],
                }
            ]

            def on_message(payload: dict) -> None:
                orderbook = parse_bitget_depth(payload)
                if orderbook:
                    self.cache.update_orderbook(orderbook)

            await self._run_depth_connection(
                name="bitget_depth",
                url=BITGET_PUBLIC_URL,
                subscribe_messages=subscribe_messages,
                on_message=on_message,
            )

    async def _mexc_ticker_loop(self, symbols: list[str]) -> None:
        subscribe_messages = [{"method": "sub.tickers", "param": {}, "gzip": False}]
        subscribe_messages.extend(
            {
                "method": "sub.ticker",
                "param": {"symbol": standard_to_exchange_symbol(symbol, "mexc", "futures")},
            }
            for symbol in symbols
        )

        def on_message(payload: dict) -> None:
            for ticker in parse_mexc_tickers(payload):
                self.cache.update_ticker(ticker)
            funding = parse_mexc_funding(payload)
            if funding:
                symbol, funding_rate, next_funding, observed_at = funding
                self.cache.update_funding(
                    exchange="mexc",
                    symbol=symbol,
                    funding_rate=funding_rate,
                    next_funding_time_utc=next_funding,
                    observed_at_utc=observed_at,
                )

        await self._json_stream_loop(
            name="mexc_ticker",
            url=MEXC_PUBLIC_URL,
            subscribe_messages=subscribe_messages,
            ping_message={"method": "ping"},
            on_message=on_message,
        )

    async def _mexc_depth_loop(self) -> None:
        while True:
            symbols = [
                target.symbol
                for target in self.cache.get_depth_targets("mexc")
            ][: self.config.depth_batch_size]
            if not symbols:
                await asyncio.sleep(1)
                continue

            subscribe_messages = [
                {
                    "method": "sub.depth.full",
                    "param": {
                        "symbol": standard_to_exchange_symbol(symbol, "mexc", "futures"),
                        "limit": 20,
                    },
                }
                for symbol in symbols
            ]

            def on_message(payload: dict) -> None:
                orderbook = parse_mexc_depth(payload)
                if orderbook:
                    self.cache.update_orderbook(orderbook)

            await self._run_depth_connection(
                name="mexc_depth",
                url=MEXC_PUBLIC_URL,
                subscribe_messages=subscribe_messages,
                ping_message={"method": "ping"},
                on_message=on_message,
            )

    async def _hyperliquid_ticker_loop(self) -> None:
        subscribe_messages = [{"method": "subscribe", "subscription": {"type": "allMids"}}]

        def on_message(payload: dict) -> None:
            for ticker in parse_hyperliquid_all_mids(payload):
                self.cache.update_ticker(ticker)
            ticker = parse_hyperliquid_bbo(payload)
            if ticker:
                self.cache.update_ticker(ticker)
            funding = parse_hyperliquid_active_asset_ctx(payload)
            if funding:
                symbol, funding_rate, next_funding, observed_at = funding
                self.cache.update_funding(
                    exchange="hyperliquid",
                    symbol=symbol,
                    funding_rate=funding_rate,
                    next_funding_time_utc=next_funding,
                    observed_at_utc=observed_at,
                )

        await self._json_stream_loop(
            name="hyperliquid_ticker",
            url=HYPERLIQUID_PUBLIC_URL,
            subscribe_messages=subscribe_messages,
            on_message=on_message,
        )

    async def _hyperliquid_depth_loop(self) -> None:
        while True:
            symbols = [
                target.symbol
                for target in self.cache.get_depth_targets("hyperliquid")
            ][: self.config.depth_batch_size]
            if not symbols:
                await asyncio.sleep(1)
                continue

            subscribe_messages = []
            for symbol in symbols:
                coin = standard_to_exchange_symbol(symbol, "hyperliquid", "futures")
                subscribe_messages.extend([
                    {"method": "subscribe", "subscription": {"type": "bbo", "coin": coin}},
                    {"method": "subscribe", "subscription": {"type": "l2Book", "coin": coin}},
                    {"method": "subscribe", "subscription": {"type": "activeAssetCtx", "coin": coin}},
                ])

            def on_message(payload: dict) -> None:
                ticker = parse_hyperliquid_bbo(payload)
                if ticker:
                    self.cache.update_ticker(ticker)
                orderbook = parse_hyperliquid_l2_book(payload)
                if orderbook:
                    self.cache.update_orderbook(orderbook)
                funding = parse_hyperliquid_active_asset_ctx(payload)
                if funding:
                    symbol, funding_rate, next_funding, observed_at = funding
                    self.cache.update_funding(
                        exchange="hyperliquid",
                        symbol=symbol,
                        funding_rate=funding_rate,
                        next_funding_time_utc=next_funding,
                        observed_at_utc=observed_at,
                    )

            await self._run_depth_connection(
                name="hyperliquid_depth",
                url=HYPERLIQUID_PUBLIC_URL,
                subscribe_messages=subscribe_messages,
                on_message=on_message,
            )

    async def _run_depth_connection(
        self,
        *,
        name: str,
        url: str,
        on_message: Callable[[dict], None],
        subscribe_messages: list[dict] | None = None,
        ping_message: dict | None = None,
    ) -> None:
        async def bounded_loop() -> None:
            await self._json_stream_loop(
                name=name,
                url=url,
                subscribe_messages=subscribe_messages,
                ping_message=ping_message,
                on_message=on_message,
            )

        try:
            await asyncio.wait_for(
                bounded_loop(),
                timeout=self.config.depth_reconnect_seconds,
            )
        except asyncio.TimeoutError:
            return


def _combined_payload(payload: dict) -> bool:
    return isinstance(payload, dict) and "stream" in payload and "data" in payload


def _chunks(items: list[str], size: int) -> list[list[str]]:
    if size <= 0:
        return [items]
    return [items[i : i + size] for i in range(0, len(items), size)]
