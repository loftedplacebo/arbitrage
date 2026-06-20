from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Callable, Iterable

from core.models import FundingInfo, OrderBook


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def age_seconds(value: datetime, now: datetime | None = None) -> float:
    now = now or utc_now()
    return max(0.0, (now - value).total_seconds())


@dataclass(frozen=True)
class CachedTicker:
    exchange: str
    symbol: str
    bid: float
    ask: float
    volume_usdt: float
    observed_at_utc: datetime
    bid_qty: float | None = None
    ask_qty: float | None = None
    funding_rate: float | None = None
    next_funding_time_utc: datetime | None = None
    source: str = "websocket"

    def is_usable(self, max_age_seconds: float, now: datetime | None = None) -> bool:
        return (
            self.bid > 0
            and self.ask > 0
            and self.ask >= self.bid
            and age_seconds(self.observed_at_utc, now=now) <= max_age_seconds
        )

    def to_fast_ticker_row(self) -> dict:
        return {
            "exchange": self.exchange,
            "symbol": self.symbol,
            "bid": self.bid,
            "ask": self.ask,
            "volume_usdt": self.volume_usdt,
            "price_source": self.source,
        }


@dataclass(frozen=True)
class DepthTarget:
    exchange: str
    symbol: str
    expires_at_utc: datetime
    source: str = "scanner"
    priority: int = 0

    def is_active(self, now: datetime | None = None) -> bool:
        return self.expires_at_utc > (now or utc_now())


@dataclass(frozen=True)
class CacheStats:
    ticker_counts: dict[str, int]
    orderbook_counts: dict[str, int]
    funding_counts: dict[str, int]
    active_depth_targets: dict[str, int]


class MarketDataCache:
    """
    Thread-safe in-memory market data cache.

    The scanner can prefer this cache for streamed data, but every getter is
    freshness-gated so REST remains the fallback whenever a stream is cold,
    stale, or incomplete.
    """

    def __init__(self):
        self._lock = RLock()
        self._tickers: dict[tuple[str, str], CachedTicker] = {}
        self._orderbooks: dict[tuple[str, str], OrderBook] = {}
        self._funding: dict[tuple[str, str], FundingInfo] = {}
        self._depth_targets: dict[tuple[str, str, str], DepthTarget] = {}
        self._listeners: list[Callable[[str, object], None]] = []

    def add_listener(self, listener: Callable[[str, object], None]) -> None:
        with self._lock:
            self._listeners.append(listener)

    def _notify_listeners(self, event_type: str, value: object) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(event_type, value)
            except Exception:
                # Cache consumers must never interrupt websocket collection.
                continue

    def update_ticker(self, ticker: CachedTicker) -> None:
        with self._lock:
            existing = self._tickers.get((ticker.exchange, ticker.symbol))
            if existing:
                ticker = CachedTicker(
                    exchange=ticker.exchange,
                    symbol=ticker.symbol,
                    bid=ticker.bid,
                    ask=ticker.ask,
                    volume_usdt=ticker.volume_usdt or existing.volume_usdt,
                    observed_at_utc=ticker.observed_at_utc,
                    bid_qty=ticker.bid_qty if ticker.bid_qty is not None else existing.bid_qty,
                    ask_qty=ticker.ask_qty if ticker.ask_qty is not None else existing.ask_qty,
                    funding_rate=(
                        ticker.funding_rate if ticker.funding_rate is not None else existing.funding_rate
                    ),
                    next_funding_time_utc=(
                        ticker.next_funding_time_utc
                        if ticker.next_funding_time_utc is not None
                        else existing.next_funding_time_utc
                    ),
                    source=ticker.source,
                )
            self._tickers[(ticker.exchange, ticker.symbol)] = ticker
            if ticker.funding_rate is not None or ticker.next_funding_time_utc is not None:
                self._funding[(ticker.exchange, ticker.symbol)] = FundingInfo(
                    exchange=ticker.exchange,
                    standard_symbol=ticker.symbol,
                    exchange_symbol=ticker.symbol,
                    funding_rate=ticker.funding_rate,
                    next_funding_time_utc=ticker.next_funding_time_utc,
                    funding_interval_hours=None,
                    observed_at_utc=ticker.observed_at_utc,
                )
        self._notify_listeners("ticker", ticker)

    def update_funding(
        self,
        *,
        exchange: str,
        symbol: str,
        funding_rate: float | None,
        next_funding_time_utc: datetime | None,
        observed_at_utc: datetime | None = None,
    ) -> None:
        observed_at_utc = observed_at_utc or utc_now()
        funding_info = FundingInfo(
            exchange=exchange,
            standard_symbol=symbol,
            exchange_symbol=symbol,
            funding_rate=funding_rate,
            next_funding_time_utc=next_funding_time_utc,
            funding_interval_hours=None,
            observed_at_utc=observed_at_utc,
        )
        with self._lock:
            self._funding[(exchange, symbol)] = funding_info
            existing = self._tickers.get((exchange, symbol))
            if not existing:
                return
            self._tickers[(exchange, symbol)] = CachedTicker(
                exchange=existing.exchange,
                symbol=existing.symbol,
                bid=existing.bid,
                ask=existing.ask,
                volume_usdt=existing.volume_usdt,
                observed_at_utc=observed_at_utc,
                bid_qty=existing.bid_qty,
                ask_qty=existing.ask_qty,
                funding_rate=funding_rate if funding_rate is not None else existing.funding_rate,
                next_funding_time_utc=next_funding_time_utc or existing.next_funding_time_utc,
                source=existing.source,
            )
        self._notify_listeners("funding", funding_info)

    def update_funding_info(self, funding_info: FundingInfo) -> None:
        with self._lock:
            self._funding[(funding_info.exchange, funding_info.standard_symbol)] = funding_info
            existing = self._tickers.get((funding_info.exchange, funding_info.standard_symbol))
            if not existing:
                return
            self._tickers[(funding_info.exchange, funding_info.standard_symbol)] = CachedTicker(
                exchange=existing.exchange,
                symbol=existing.symbol,
                bid=existing.bid,
                ask=existing.ask,
                volume_usdt=existing.volume_usdt,
                observed_at_utc=existing.observed_at_utc,
                bid_qty=existing.bid_qty,
                ask_qty=existing.ask_qty,
                funding_rate=funding_info.funding_rate,
                next_funding_time_utc=funding_info.next_funding_time_utc,
                source=existing.source,
            )
        self._notify_listeners("funding", funding_info)

    def update_orderbook(self, orderbook: OrderBook) -> None:
        with self._lock:
            self._orderbooks[(orderbook.exchange, orderbook.standard_symbol)] = orderbook
        self._notify_listeners("orderbook", orderbook)

    def get_fast_tickers(
        self,
        exchange: str,
        *,
        max_age_seconds: float,
        min_count: int = 1,
    ) -> dict[str, dict]:
        now = utc_now()
        with self._lock:
            rows = {
                symbol: ticker.to_fast_ticker_row()
                for (ticker_exchange, symbol), ticker in self._tickers.items()
                if ticker_exchange == exchange and ticker.is_usable(max_age_seconds, now=now)
            }
        if len(rows) < min_count:
            return {}
        return rows

    def get_symbol_tickers(
        self,
        symbol: str,
        *,
        max_age_seconds: float,
    ) -> dict[str, dict]:
        now = utc_now()
        with self._lock:
            return {
                exchange: ticker.to_fast_ticker_row()
                for (exchange, ticker_symbol), ticker in self._tickers.items()
                if ticker_symbol == symbol and ticker.is_usable(max_age_seconds, now=now)
            }

    def get_orderbook(
        self,
        exchange: str,
        symbol: str,
        *,
        max_age_seconds: float,
    ) -> OrderBook | None:
        now = utc_now()
        with self._lock:
            orderbook = self._orderbooks.get((exchange, symbol))
        if orderbook is None:
            return None
        if age_seconds(orderbook.observed_at_utc, now=now) > max_age_seconds:
            return None
        if not orderbook.bids or not orderbook.asks:
            return None
        return orderbook

    def get_funding_info(
        self,
        exchange: str,
        symbol: str,
        *,
        max_age_seconds: float,
    ) -> FundingInfo | None:
        now = utc_now()
        with self._lock:
            funding_info = self._funding.get((exchange, symbol))
        if funding_info is None:
            return None
        if age_seconds(funding_info.observed_at_utc, now=now) > max_age_seconds:
            return None
        return funding_info

    def set_depth_targets(
        self,
        targets: Iterable[tuple[str, str]],
        *,
        ttl_seconds: float,
        source: str = "scanner",
        priority: int = 0,
    ) -> None:
        expires_at = utc_now() + timedelta(seconds=ttl_seconds)
        with self._lock:
            for exchange, symbol in targets:
                self._depth_targets[(source, exchange, symbol)] = DepthTarget(
                    exchange=exchange,
                    symbol=symbol,
                    expires_at_utc=expires_at,
                    source=source,
                    priority=priority,
                )
            self._prune_depth_targets_locked()

    def replace_depth_targets(
        self,
        targets: Iterable[tuple[str, str]],
        *,
        ttl_seconds: float,
        source: str = "scanner",
        priority: int = 0,
    ) -> None:
        expires_at = utc_now() + timedelta(seconds=ttl_seconds)
        with self._lock:
            self._depth_targets = {
                key: target
                for key, target in self._depth_targets.items()
                if target.source != source
            }
            self._depth_targets.update({
                (source, exchange, symbol): DepthTarget(
                    exchange=exchange,
                    symbol=symbol,
                    expires_at_utc=expires_at,
                    source=source,
                    priority=priority,
                )
                for exchange, symbol in targets
            })

    def get_depth_targets(self, exchange: str | None = None) -> list[DepthTarget]:
        now = utc_now()
        with self._lock:
            self._prune_depth_targets_locked(now=now)
            raw_targets = [
                target
                for target in self._depth_targets.values()
                if target.is_active(now) and (exchange is None or target.exchange == exchange)
            ]
        merged: dict[tuple[str, str], DepthTarget] = {}
        for target in raw_targets:
            key = (target.exchange, target.symbol)
            existing = merged.get(key)
            if existing is None or (target.priority, target.expires_at_utc) > (
                existing.priority,
                existing.expires_at_utc,
            ):
                merged[key] = target
        return sorted(
            merged.values(),
            key=lambda item: (-item.priority, item.exchange, item.symbol),
        )

    def has_depth_target(
        self,
        exchange: str,
        symbol: str,
        *,
        source: str | None = None,
    ) -> bool:
        now = utc_now()
        with self._lock:
            self._prune_depth_targets_locked(now=now)
            return any(
                target.exchange == exchange
                and target.symbol == symbol
                and (source is None or target.source == source)
                and target.is_active(now)
                for target in self._depth_targets.values()
            )

    def get_ticker_symbols(
        self,
        exchange: str,
        *,
        max_age_seconds: float,
    ) -> list[str]:
        now = utc_now()
        with self._lock:
            symbols = [
                symbol
                for (ticker_exchange, symbol), ticker in self._tickers.items()
                if ticker_exchange == exchange and ticker.is_usable(max_age_seconds, now=now)
            ]
        return sorted(symbols)

    def stats(self) -> CacheStats:
        with self._lock:
            ticker_counts: dict[str, int] = {}
            for exchange, _symbol in self._tickers:
                ticker_counts[exchange] = ticker_counts.get(exchange, 0) + 1

            orderbook_counts: dict[str, int] = {}
            for exchange, _symbol in self._orderbooks:
                orderbook_counts[exchange] = orderbook_counts.get(exchange, 0) + 1

            funding_counts: dict[str, int] = {}
            for exchange, _symbol in self._funding:
                funding_counts[exchange] = funding_counts.get(exchange, 0) + 1

            self._prune_depth_targets_locked()
            target_counts: dict[str, int] = {}
            for target in self.get_depth_targets():
                target_counts[target.exchange] = target_counts.get(target.exchange, 0) + 1

        return CacheStats(
            ticker_counts=ticker_counts,
            orderbook_counts=orderbook_counts,
            funding_counts=funding_counts,
            active_depth_targets=target_counts,
        )

    def _prune_depth_targets_locked(self, now: datetime | None = None) -> None:
        now = now or utc_now()
        expired = [
            key
            for key, target in self._depth_targets.items()
            if not target.is_active(now)
        ]
        for key in expired:
            self._depth_targets.pop(key, None)
