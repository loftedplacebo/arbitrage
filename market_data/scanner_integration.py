from __future__ import annotations

import time
from typing import Iterable

from market_data.cache import MarketDataCache


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
