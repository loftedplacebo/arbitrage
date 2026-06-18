from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from market_data.cache import MarketDataCache


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def candidate_route_key(row: dict) -> str:
    return "|".join([
        str(row.get("symbol", "")),
        str(row.get("long_exchange", "")),
        str(row.get("short_exchange", "")),
        str(row.get("direction", "")),
    ])


@dataclass
class WatchlistItem:
    key: str
    candidate: dict
    first_seen_utc: datetime
    last_seen_utc: datetime
    best_spread_pct: float
    seen_count: int = 1
    reason: str = "fast_candidate"
    priority_bonus: float = 0.0

    def age_seconds(self, now: datetime) -> float:
        return max(0.0, (now - self.last_seen_utc).total_seconds())


class CandidateWatchlist:
    """
    Short-lived route memory for websocket depth warming.

    The scanner still validates fresh candidates conservatively, but routes
    that were recently interesting stay subscribed for depth so later scans
    are less likely to fall back to REST or wait on cold order books.
    """

    def __init__(self, *, ttl_seconds: float, max_routes: int):
        self.ttl_seconds = ttl_seconds
        self.max_routes = max_routes
        self._items: dict[str, WatchlistItem] = {}

    def __len__(self) -> int:
        self.prune()
        return len(self._items)

    def add_candidate(
        self,
        candidate: dict,
        *,
        observed_at_utc: datetime | None = None,
        reason: str = "fast_candidate",
        priority_bonus: float = 0.0,
    ) -> None:
        key = candidate_route_key(candidate)
        if key.count("|") != 3 or not candidate.get("symbol"):
            return

        observed_at_utc = observed_at_utc or utc_now()
        spread = candidate.get("fast_spread_pct")
        try:
            spread_value = float(spread) if spread is not None else 0.0
        except (TypeError, ValueError):
            spread_value = 0.0

        existing = self._items.get(key)
        if existing is None:
            self._items[key] = WatchlistItem(
                key=key,
                candidate=dict(candidate),
                first_seen_utc=observed_at_utc,
                last_seen_utc=observed_at_utc,
                best_spread_pct=spread_value,
                seen_count=1,
                reason=reason,
                priority_bonus=priority_bonus,
            )
        else:
            existing.candidate = {**existing.candidate, **candidate}
            existing.last_seen_utc = observed_at_utc
            existing.best_spread_pct = max(existing.best_spread_pct, spread_value)
            existing.seen_count += 1
            if priority_bonus >= existing.priority_bonus:
                existing.reason = reason
                existing.priority_bonus = priority_bonus

        self.prune(now=observed_at_utc)

    def add_candidates(
        self,
        candidates: Iterable[dict],
        *,
        observed_at_utc: datetime | None = None,
        reason: str = "fast_candidate",
        priority_bonus: float = 0.0,
        max_candidates: int | None = None,
    ) -> None:
        rows = list(candidates)
        if max_candidates is not None:
            rows = rows[:max_candidates]
        for candidate in rows:
            self.add_candidate(
                candidate,
                observed_at_utc=observed_at_utc,
                reason=reason,
                priority_bonus=priority_bonus,
            )

    def metadata_for(self, candidate: dict, now: datetime | None = None) -> dict:
        now = now or utc_now()
        item = self._items.get(candidate_route_key(candidate))
        if item is None or item.age_seconds(now) > self.ttl_seconds:
            return {}
        return {
            "watchlist_reason": item.reason,
            "watchlist_seen_count": item.seen_count,
            "watchlist_age_seconds": item.age_seconds(now),
            "watchlist_best_spread_pct": item.best_spread_pct,
            "watchlist_priority_bonus": item.priority_bonus,
        }

    def candidates(self, *, now: datetime | None = None) -> list[dict]:
        now = now or utc_now()
        self.prune(now=now)
        rows = []
        for item in self._items.values():
            row = dict(item.candidate)
            row.update(self.metadata_for(row, now=now))
            rows.append(row)
        rows.sort(
            key=lambda row: (
                row.get("watchlist_priority_bonus") or 0.0,
                row.get("watchlist_best_spread_pct") or 0.0,
                row.get("watchlist_seen_count") or 0,
                -(row.get("watchlist_age_seconds") or 0.0),
            ),
            reverse=True,
        )
        return rows

    def prune(self, *, now: datetime | None = None) -> None:
        now = now or utc_now()
        expired = [
            key
            for key, item in self._items.items()
            if item.age_seconds(now) > self.ttl_seconds
        ]
        for key in expired:
            self._items.pop(key, None)

        if len(self._items) <= self.max_routes:
            return

        ranked = sorted(
            self._items.items(),
            key=lambda pair: (
                pair[1].priority_bonus,
                pair[1].best_spread_pct,
                pair[1].seen_count,
                pair[1].last_seen_utc,
            ),
            reverse=True,
        )
        self._items = dict(ranked[: self.max_routes])


def build_depth_targets_from_candidates(
    candidates: Iterable[dict],
    *,
    max_candidates: int,
) -> list[tuple[str, str]]:
    targets: set[tuple[str, str]] = set()
    for candidate in list(candidates)[:max_candidates]:
        symbol = candidate.get("symbol")
        long_exchange = candidate.get("long_exchange")
        short_exchange = candidate.get("short_exchange")
        if not symbol:
            continue
        if long_exchange:
            targets.add((str(long_exchange), str(symbol)))
        if short_exchange:
            targets.add((str(short_exchange), str(symbol)))
    return sorted(targets)


def get_ticker_data_with_cache(
    *,
    adapters: dict,
    cache: MarketDataCache | None,
    max_age_seconds: float,
    min_cached_tickers: int,
) -> tuple[dict[str, dict[str, dict]], dict[str, str]]:
    ticker_data = {}
    source_by_exchange = {}

    for name, adapter in adapters.items():
        cached = {}
        if cache is not None:
            cached = cache.get_fast_tickers(
                name,
                max_age_seconds=max_age_seconds,
                min_count=min_cached_tickers,
            )

        if cached:
            ticker_data[name] = cached
            source_by_exchange[name] = "websocket"
            continue

        tickers = adapter.get_fast_futures_tickers()
        ticker_data[name] = tickers
        source_by_exchange[name] = "rest"

    return ticker_data, source_by_exchange


def get_cached_orderbook(
    *,
    cache: MarketDataCache | None,
    exchange: str,
    symbol: str,
    max_age_seconds: float,
):
    if cache is None:
        return None
    return cache.get_orderbook(
        exchange,
        symbol,
        max_age_seconds=max_age_seconds,
    )


def get_funding_info_with_cache(
    *,
    cache: MarketDataCache | None,
    adapter,
    exchange: str,
    symbol: str,
    max_age_seconds: float,
):
    if cache is not None:
        cached = cache.get_funding_info(
            exchange,
            symbol,
            max_age_seconds=max_age_seconds,
        )
        if cached is not None:
            return cached

    funding_info = adapter.get_funding_info(symbol)
    if cache is not None:
        cache.update_funding_info(funding_info)
    return funding_info


def count_candidate_orderbook_coverage(
    *,
    candidates: Iterable[dict],
    cache: MarketDataCache | None,
    max_age_seconds: float,
) -> tuple[int, int]:
    if cache is None:
        return 0, 0

    rows = list(candidates)
    ready = 0
    for candidate in rows:
        symbol = candidate.get("symbol")
        long_exchange = candidate.get("long_exchange")
        short_exchange = candidate.get("short_exchange")
        if not symbol or not long_exchange or not short_exchange:
            continue

        long_book = cache.get_orderbook(
            str(long_exchange),
            str(symbol),
            max_age_seconds=max_age_seconds,
        )
        short_book = cache.get_orderbook(
            str(short_exchange),
            str(symbol),
            max_age_seconds=max_age_seconds,
        )
        if long_book is not None and short_book is not None:
            ready += 1

    return ready, len(rows)


def wait_for_candidate_orderbooks(
    *,
    candidates: Iterable[dict],
    cache: MarketDataCache | None,
    timeout_seconds: float,
    poll_seconds: float,
    max_age_seconds: float,
) -> tuple[int, int]:
    rows = list(candidates)
    if not rows or cache is None or timeout_seconds <= 0:
        return count_candidate_orderbook_coverage(
            candidates=rows,
            cache=cache,
            max_age_seconds=max_age_seconds,
        )

    deadline = time.monotonic() + timeout_seconds
    while True:
        ready, total = count_candidate_orderbook_coverage(
            candidates=rows,
            cache=cache,
            max_age_seconds=max_age_seconds,
        )
        if ready >= total:
            return ready, total
        if time.monotonic() >= deadline:
            return ready, total
        time.sleep(max(0.05, poll_seconds))
