from __future__ import annotations

import asyncio
import itertools
import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4
from typing import Awaitable, Callable, Iterable

import requests

from core.symbols import standard_to_exchange_symbol
from market_data.cache import DepthTarget, MarketDataCache
from market_data.ws_parsers import (
    parse_binance_book_ticker,
    parse_binance_depth,
    parse_binance_mark_price_updates,
    parse_bitget_depth,
    parse_bitget_tickers,
    parse_hyperliquid_active_asset_ctx,
    parse_hyperliquid_bbo,
    parse_hyperliquid_l2_book,
    parse_kucoin_depth,
    parse_kucoin_ticker,
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
BINANCE_PUBLIC_WS_URL = "wss://fstream.binance.com/public/ws"
BINANCE_PUBLIC_COMBINED_BASE_URL = "wss://fstream.binance.com/public/stream?streams="
BITGET_PUBLIC_URL = "wss://ws.bitget.com/v2/ws/public"
MEXC_PUBLIC_URL = "wss://contract.mexc.com/edge"
HYPERLIQUID_PUBLIC_URL = "wss://api.hyperliquid.xyz/ws"
KUCOIN_FUTURES_BULLET_URL = "https://api-futures.kucoin.com/api/v1/bullet-public"


@dataclass(frozen=True)
class WebsocketRuntimeConfig:
    enabled_exchanges: set[str] = field(
        default_factory=lambda: {"binance", "bitget", "mexc", "kucoin", "hyperliquid"}
    )
    ticker_symbol_limit: int | None = None
    ticker_batch_size: int = 100
    depth_batch_size: int = 80
    depth_target_ttl_seconds: float = 120.0
    depth_target_refresh_seconds: float = 0.25
    depth_reconnect_seconds: float = 5.0
    reconnect_delay_seconds: float = 5.0
    ping_interval_seconds: float = 15.0
    funding_reconcile_enabled: bool = True
    funding_reconcile_seconds: float = 180.0
    funding_reconcile_symbol_limit: int = 80
    funding_reconcile_request_sleep_seconds: float = 0.05


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
        self._message_ids = itertools.count(1)
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

    def set_depth_targets(
        self,
        candidates: Iterable[dict],
        *,
        max_candidates: int,
        replace: bool = False,
        depth_tier: str = "deep",
        source: str = "scanner",
        priority: int = 0,
        ttl_seconds: float | None = None,
    ) -> None:
        from market_data.scanner_integration import build_depth_targets_from_candidates

        targets = build_depth_targets_from_candidates(
            candidates,
            max_candidates=max_candidates,
        )
        if replace:
            self.cache.replace_depth_targets(
                targets,
                ttl_seconds=ttl_seconds or self.config.depth_target_ttl_seconds,
                source=source,
                priority=priority,
                depth_tier=depth_tier,
            )
        else:
            self.cache.set_depth_targets(
                targets,
                ttl_seconds=ttl_seconds or self.config.depth_target_ttl_seconds,
                source=source,
                priority=priority,
                depth_tier=depth_tier,
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
        if "kucoin" in enabled and "kucoin" in self.adapters:
            symbols = self._limited_symbols("kucoin")
            tasks.extend([
                asyncio.create_task(self._kucoin_ticker_loop(symbols)),
                asyncio.create_task(self._kucoin_depth_loop()),
            ])
        if "hyperliquid" in enabled and "hyperliquid" in self.adapters:
            symbols = self._limited_symbols("hyperliquid")
            tasks.extend([
                asyncio.create_task(self._hyperliquid_ticker_loop(symbols)),
                asyncio.create_task(self._hyperliquid_depth_loop()),
            ])
        if self.config.funding_reconcile_enabled:
            tasks.append(asyncio.create_task(self._funding_reconciliation_loop()))

        await self._stop_event.wait()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def _limited_symbols(self, exchange: str) -> list[str]:
        adapter = self.adapters[exchange]
        try:
            symbols = sorted(adapter.get_futures_usdt_symbols())
        except Exception as exc:
            print(f"[ws:{exchange}_symbols] unavailable: {exc}")
            return []
        if self.config.ticker_symbol_limit is not None:
            symbols = symbols[: self.config.ticker_symbol_limit]
        return symbols

    async def _json_stream_loop(
        self,
        *,
        name: str,
        url: str | Callable[[], str | Awaitable[str]],
        on_message: Callable[[dict], None],
        subscribe_messages: list[dict] | None = None,
        ping_message: dict | None = None,
    ) -> None:
        assert self._stop_event is not None
        while not self._stop_event.is_set():
            try:
                stream_url = await self._resolve_stream_url(url)
                async with websockets.connect(stream_url, ping_interval=None) as ws:
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

    async def _dynamic_json_subscription_loop(
        self,
        *,
        name: str,
        exchange: str,
        url: str | Callable[[], str | Awaitable[str]],
        on_message: Callable[[dict], None],
        subscribe_messages: Callable[[list[DepthTarget | str]], list[dict]],
        unsubscribe_messages: Callable[[list[DepthTarget | str]], list[dict]],
        ping_message: dict | None = None,
    ) -> None:
        assert self._stop_event is not None
        while not self._stop_event.is_set():
            try:
                stream_url = await self._resolve_stream_url(url)
                async with websockets.connect(stream_url, ping_interval=None) as ws:
                    subscribed: set[str] = set()
                    last_ping = asyncio.get_running_loop().time()
                    next_target_refresh = 0.0

                    while not self._stop_event.is_set():
                        now = asyncio.get_running_loop().time()

                        if now >= next_target_refresh:
                            desired_targets = self._coalesce_depth_targets_for_subscription(
                                self.cache.get_depth_targets(exchange)
                            )[: self.config.depth_batch_size]
                            desired = {
                                target.subscription_key: target
                                for target in desired_targets
                            }
                            desired_set = set(desired)
                            to_unsubscribe_keys = sorted(subscribed - desired_set)
                            to_subscribe_keys = sorted(desired_set - subscribed)
                            to_unsubscribe = [
                                self._target_from_subscription_key(key)
                                for key in to_unsubscribe_keys
                            ]
                            to_subscribe = [desired[key] for key in to_subscribe_keys]

                            for message in unsubscribe_messages(to_unsubscribe):
                                await ws.send(json.dumps(message))
                            for message in subscribe_messages(to_subscribe):
                                await ws.send(json.dumps(message))

                            subscribed.difference_update(to_unsubscribe_keys)
                            subscribed.update(to_subscribe_keys)
                            next_target_refresh = now + self.config.depth_target_refresh_seconds

                        if ping_message and (
                            now - last_ping
                        ) >= self.config.ping_interval_seconds:
                            await ws.send(json.dumps(ping_message))
                            last_ping = now

                        timeout = min(
                            1.0,
                            max(0.05, next_target_refresh - asyncio.get_running_loop().time()),
                        )
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                        except asyncio.TimeoutError:
                            continue
                        payload = json.loads(raw)
                        on_message(payload.get("data") if _combined_payload(payload) else payload)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"[ws:{name}] disconnected: {exc}")
                await asyncio.sleep(self.config.reconnect_delay_seconds)

    async def _resolve_stream_url(self, url: str | Callable[[], str | Awaitable[str]]) -> str:
        if not callable(url):
            return url
        resolved = url()
        if asyncio.iscoroutine(resolved):
            return await resolved
        return resolved

    def _target_symbol(self, target: DepthTarget | str) -> str:
        return target.symbol if isinstance(target, DepthTarget) else str(target)

    def _target_depth_tier(self, target: DepthTarget | str) -> str:
        return target.depth_tier if isinstance(target, DepthTarget) else "deep"

    def _coalesce_depth_targets_for_subscription(
        self,
        targets: list[DepthTarget],
    ) -> list[DepthTarget]:
        best_by_symbol: dict[str, DepthTarget] = {}
        for target in targets:
            existing = best_by_symbol.get(target.symbol)
            if existing is None or self._depth_target_rank(target) > self._depth_target_rank(existing):
                best_by_symbol[target.symbol] = target
        return sorted(
            best_by_symbol.values(),
            key=lambda item: self._depth_target_rank(item),
            reverse=True,
        )

    def _depth_target_rank(self, target: DepthTarget) -> tuple[int, int, float]:
        tier_rank = 1 if target.depth_tier == "deep" else 0
        return (target.priority, tier_rank, target.expires_at_utc.timestamp())

    def _target_from_subscription_key(self, key: str) -> DepthTarget:
        exchange, symbol, depth_tier = key.split("|", 2)
        return DepthTarget(
            exchange=exchange,
            symbol=symbol,
            expires_at_utc=datetime.now(timezone.utc),
            depth_tier=depth_tier,
        )

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

    def _binance_depth_stream(self, target: DepthTarget | str) -> str:
        symbol = self._target_symbol(target).lower()
        levels = "depth5" if self._target_depth_tier(target) == "shallow" else "depth20"
        return f"{symbol}@{levels}@100ms"

    def _binance_depth_subscribe_messages(self, targets: list[DepthTarget | str]) -> list[dict]:
        if not targets:
            return []
        return [{
            "method": "SUBSCRIBE",
            "params": [self._binance_depth_stream(target) for target in targets],
            "id": next(self._message_ids),
        }]

    def _binance_depth_unsubscribe_messages(self, targets: list[DepthTarget | str]) -> list[dict]:
        if not targets:
            return []
        return [{
            "method": "UNSUBSCRIBE",
            "params": [self._binance_depth_stream(target) for target in targets],
            "id": next(self._message_ids),
        }]

    async def _binance_depth_loop(self) -> None:
        def on_message(payload: dict) -> None:
            orderbook = parse_binance_depth(payload)
            if orderbook:
                self.cache.update_orderbook(orderbook)

        await self._dynamic_json_subscription_loop(
            name="binance_depth",
            exchange="binance",
            url=BINANCE_PUBLIC_WS_URL,
            subscribe_messages=self._binance_depth_subscribe_messages,
            unsubscribe_messages=self._binance_depth_unsubscribe_messages,
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

    def _bitget_depth_args(self, targets: list[DepthTarget | str]) -> list[dict]:
        return [
            {
                "instType": "USDT-FUTURES",
                "channel": "books5" if self._target_depth_tier(target) == "shallow" else "books15",
                "instId": self._target_symbol(target),
            }
            for target in targets
        ]

    def _bitget_depth_subscribe_messages(self, targets: list[DepthTarget | str]) -> list[dict]:
        return [
            {"op": "subscribe", "args": self._bitget_depth_args(batch)}
            for batch in _chunks(targets, min(50, max(1, self.config.depth_batch_size)))
            if batch
        ]

    def _bitget_depth_unsubscribe_messages(self, targets: list[DepthTarget | str]) -> list[dict]:
        return [
            {"op": "unsubscribe", "args": self._bitget_depth_args(batch)}
            for batch in _chunks(targets, min(50, max(1, self.config.depth_batch_size)))
            if batch
        ]

    async def _bitget_depth_loop(self) -> None:
        def on_message(payload: dict) -> None:
            orderbook = parse_bitget_depth(payload)
            if orderbook:
                self.cache.update_orderbook(orderbook)

        await self._dynamic_json_subscription_loop(
            name="bitget_depth",
            exchange="bitget",
            url=BITGET_PUBLIC_URL,
            subscribe_messages=self._bitget_depth_subscribe_messages,
            unsubscribe_messages=self._bitget_depth_unsubscribe_messages,
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

    def _mexc_depth_limit(self, target: DepthTarget | str) -> int:
        return 5 if self._target_depth_tier(target) == "shallow" else 20

    def _mexc_depth_subscribe_messages(self, targets: list[DepthTarget | str]) -> list[dict]:
        return [
            {
                "method": "sub.depth.full",
                "param": {
                    "symbol": standard_to_exchange_symbol(
                        self._target_symbol(target),
                        "mexc",
                        "futures",
                    ),
                    "limit": self._mexc_depth_limit(target),
                },
            }
            for target in targets
        ]

    def _mexc_depth_unsubscribe_messages(self, targets: list[DepthTarget | str]) -> list[dict]:
        return [
            {
                "method": "unsub.depth.full",
                "param": {
                    "symbol": standard_to_exchange_symbol(
                        self._target_symbol(target),
                        "mexc",
                        "futures",
                    ),
                    "limit": self._mexc_depth_limit(target),
                },
            }
            for target in targets
        ]

    async def _mexc_depth_loop(self) -> None:
        def on_message(payload: dict) -> None:
            orderbook = parse_mexc_depth(payload)
            if orderbook:
                self.cache.update_orderbook(orderbook)

        await self._dynamic_json_subscription_loop(
            name="mexc_depth",
            exchange="mexc",
            url=MEXC_PUBLIC_URL,
            subscribe_messages=self._mexc_depth_subscribe_messages,
            unsubscribe_messages=self._mexc_depth_unsubscribe_messages,
            ping_message={"method": "ping"},
            on_message=on_message,
        )

    async def _kucoin_public_ws_url(self) -> str:
        return await asyncio.to_thread(self._kucoin_public_ws_url_sync)

    def _kucoin_public_ws_url_sync(self) -> str:
        response = requests.post(KUCOIN_FUTURES_BULLET_URL, timeout=20)
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") != "200000":
            raise RuntimeError(f"KuCoin websocket token error: {payload}")

        data = payload.get("data") or {}
        token = data.get("token")
        servers = data.get("instanceServers") or []
        if not token or not servers:
            raise RuntimeError(f"KuCoin websocket token response missing server data: {payload}")

        endpoint = servers[0].get("endpoint")
        if not endpoint:
            raise RuntimeError(f"KuCoin websocket token response missing endpoint: {payload}")

        return f"{endpoint}?token={token}&connectId={uuid4().hex}"

    def _kucoin_subscribe_message(self, topic: str) -> dict:
        return {
            "id": uuid4().hex,
            "type": "subscribe",
            "topic": topic,
            "privateChannel": False,
            "response": True,
        }

    def _kucoin_unsubscribe_message(self, topic: str) -> dict:
        return {
            "id": uuid4().hex,
            "type": "unsubscribe",
            "topic": topic,
            "privateChannel": False,
            "response": True,
        }

    async def _kucoin_ticker_loop(self, symbols: list[str]) -> None:
        assert self._stop_event is not None
        if not symbols:
            await self._stop_event.wait()
            return

        tasks = [
            asyncio.create_task(self._kucoin_ticker_batch_loop(batch, batch_number=index))
            for index, batch in enumerate(_chunks(symbols, self.config.ticker_batch_size), start=1)
        ]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _kucoin_ticker_batch_loop(self, symbols: list[str], *, batch_number: int) -> None:
        subscribe_messages = [
            self._kucoin_subscribe_message(
                f"/contractMarket/tickerV2:{standard_to_exchange_symbol(symbol, 'kucoin', 'futures')}"
            )
            for symbol in symbols
        ]

        def on_message(payload: dict) -> None:
            ticker = parse_kucoin_ticker(payload)
            if ticker:
                self.cache.update_ticker(ticker)

        await self._json_stream_loop(
            name=f"kucoin_ticker_{batch_number}",
            url=self._kucoin_public_ws_url,
            subscribe_messages=subscribe_messages,
            ping_message={"id": "ping", "type": "ping"},
            on_message=on_message,
        )

    def _kucoin_depth_topic(self, target: DepthTarget | str) -> str:
        symbol = self._target_symbol(target)
        return (
            "/contractMarket/level2Depth50:"
            f"{standard_to_exchange_symbol(symbol, 'kucoin', 'futures')}"
        )

    def _kucoin_depth_subscribe_messages(self, targets: list[DepthTarget | str]) -> list[dict]:
        return [
            self._kucoin_subscribe_message(self._kucoin_depth_topic(target))
            for target in targets
        ]

    def _kucoin_depth_unsubscribe_messages(self, targets: list[DepthTarget | str]) -> list[dict]:
        return [
            self._kucoin_unsubscribe_message(self._kucoin_depth_topic(target))
            for target in targets
        ]

    async def _kucoin_depth_loop(self) -> None:
        def on_message(payload: dict) -> None:
            orderbook = parse_kucoin_depth(payload)
            if orderbook:
                self.cache.update_orderbook(orderbook)

        await self._dynamic_json_subscription_loop(
            name="kucoin_depth",
            exchange="kucoin",
            url=self._kucoin_public_ws_url,
            subscribe_messages=self._kucoin_depth_subscribe_messages,
            unsubscribe_messages=self._kucoin_depth_unsubscribe_messages,
            ping_message={"id": "ping", "type": "ping"},
            on_message=on_message,
        )

    async def _hyperliquid_ticker_loop(self, symbols: list[str]) -> None:
        assert self._stop_event is not None
        if not symbols:
            await self._stop_event.wait()
            return

        tasks = [
            asyncio.create_task(self._hyperliquid_ticker_batch_loop(batch, batch_number=index))
            for index, batch in enumerate(
                _chunks(symbols, min(self.config.ticker_batch_size, 50)),
                start=1,
            )
        ]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _hyperliquid_ticker_batch_loop(self, symbols: list[str], *, batch_number: int) -> None:
        subscribe_messages = []
        for symbol in symbols:
            coin = standard_to_exchange_symbol(symbol, "hyperliquid", "futures")
            subscribe_messages.append(
                {"method": "subscribe", "subscription": {"type": "bbo", "coin": coin}}
            )

        def on_message(payload: dict) -> None:
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
            name=f"hyperliquid_ticker_{batch_number}",
            url=HYPERLIQUID_PUBLIC_URL,
            subscribe_messages=subscribe_messages,
            on_message=on_message,
        )

    def _hyperliquid_depth_subscription_messages(
        self,
        targets: list[DepthTarget | str],
        method: str,
    ) -> list[dict]:
        messages = []
        for target in targets:
            symbol = self._target_symbol(target)
            coin = standard_to_exchange_symbol(symbol, "hyperliquid", "futures")
            messages.extend([
                {"method": method, "subscription": {"type": "bbo", "coin": coin}},
                {"method": method, "subscription": {"type": "l2Book", "coin": coin}},
                {"method": method, "subscription": {"type": "activeAssetCtx", "coin": coin}},
            ])
        return messages

    def _hyperliquid_depth_subscribe_messages(self, targets: list[DepthTarget | str]) -> list[dict]:
        return self._hyperliquid_depth_subscription_messages(targets, "subscribe")

    def _hyperliquid_depth_unsubscribe_messages(self, targets: list[DepthTarget | str]) -> list[dict]:
        return self._hyperliquid_depth_subscription_messages(targets, "unsubscribe")

    async def _hyperliquid_depth_loop(self) -> None:
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

        await self._dynamic_json_subscription_loop(
            name="hyperliquid_depth",
            exchange="hyperliquid",
            url=HYPERLIQUID_PUBLIC_URL,
            subscribe_messages=self._hyperliquid_depth_subscribe_messages,
            unsubscribe_messages=self._hyperliquid_depth_unsubscribe_messages,
            on_message=on_message,
        )

    async def _funding_reconciliation_loop(self) -> None:
        assert self._stop_event is not None
        while not self._stop_event.is_set():
            try:
                await self._reconcile_funding_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"[ws:funding_reconcile] error: {exc}")

            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self.config.funding_reconcile_seconds,
                )
            except asyncio.TimeoutError:
                continue

    async def _reconcile_funding_once(self) -> None:
        for exchange, adapter in sorted(self.adapters.items()):
            if exchange not in self.config.enabled_exchanges:
                continue
            if not hasattr(adapter, "get_funding_info"):
                continue

            symbols = self._funding_reconcile_symbols(exchange)
            if not symbols:
                continue

            failures = 0
            for symbol in symbols:
                try:
                    funding_info = await asyncio.to_thread(adapter.get_funding_info, symbol)
                    self.cache.update_funding_info(funding_info)
                except Exception:
                    failures += 1

                if self.config.funding_reconcile_request_sleep_seconds > 0:
                    await asyncio.sleep(self.config.funding_reconcile_request_sleep_seconds)

            if failures:
                print(
                    f"[ws:funding_reconcile] {exchange} funding failures: "
                    f"{failures}/{len(symbols)}"
                )

    def _funding_reconcile_symbols(self, exchange: str) -> list[str]:
        depth_symbols = [target.symbol for target in self.cache.get_depth_targets(exchange)]
        ticker_symbols = self.cache.get_ticker_symbols(exchange, max_age_seconds=300)

        seen = set()
        symbols = []
        for symbol in depth_symbols + ticker_symbols:
            if symbol in seen:
                continue
            seen.add(symbol)
            symbols.append(symbol)

        return symbols[: self.config.funding_reconcile_symbol_limit]

    async def _run_depth_connection(
        self,
        *,
        name: str,
        url: str | Callable[[], str | Awaitable[str]],
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
