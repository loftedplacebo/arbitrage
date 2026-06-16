from __future__ import annotations

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
