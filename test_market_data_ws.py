from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core.models import FundingInfo, OrderBook, OrderBookLevel
from market_data.cache import CachedTicker, MarketDataCache
from market_data.scanner_integration import (
    CandidateWatchlist,
    build_depth_targets_from_candidates,
    candidate_route_key,
    count_candidate_orderbook_coverage,
    get_funding_info_with_cache,
    get_ticker_data_with_cache,
    wait_for_candidate_orderbooks,
)
from scanners.fast_futures_futures_scanner import (
    build_depth_warm_candidates,
    build_fast_candidates,
    deep_validate_candidate,
    select_deep_candidates,
)
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
    parse_kucoin_depth,
    parse_kucoin_ticker,
    parse_mexc_depth,
    parse_mexc_funding,
    parse_mexc_tickers,
)


def test_cache_returns_only_fresh_usable_tickers():
    cache = MarketDataCache()
    now = datetime.now(timezone.utc)
    cache.update_ticker(
        CachedTicker(
            exchange="binance",
            symbol="BTCUSDT",
            bid=100.0,
            ask=101.0,
            volume_usdt=1_000_000.0,
            observed_at_utc=now,
        )
    )
    cache.update_ticker(
        CachedTicker(
            exchange="binance",
            symbol="ETHUSDT",
            bid=100.0,
            ask=101.0,
            volume_usdt=1_000_000.0,
            observed_at_utc=now - timedelta(seconds=60),
        )
    )

    rows = cache.get_fast_tickers("binance", max_age_seconds=10)

    assert set(rows) == {"BTCUSDT"}
    assert rows["BTCUSDT"]["bid"] == 100.0


def test_depth_targets_expire():
    cache = MarketDataCache()
    cache.set_depth_targets(
        [("binance", "BTCUSDT"), ("bitget", "BTCUSDT")],
        ttl_seconds=60,
    )

    assert [target.symbol for target in cache.get_depth_targets("binance")] == ["BTCUSDT"]
    assert len(cache.get_depth_targets()) == 2


def test_replace_depth_targets_removes_stale_targets():
    cache = MarketDataCache()
    cache.set_depth_targets(
        [("binance", "BTCUSDT"), ("bitget", "BTCUSDT")],
        ttl_seconds=60,
    )
    cache.replace_depth_targets(
        [("mexc", "ETHUSDT")],
        ttl_seconds=60,
    )

    targets = cache.get_depth_targets()

    assert [(target.exchange, target.symbol) for target in targets] == [("mexc", "ETHUSDT")]


def test_build_depth_targets_from_candidates_limits_and_dedupes():
    candidates = [
        {"symbol": "BTCUSDT", "long_exchange": "binance", "short_exchange": "bitget"},
        {"symbol": "BTCUSDT", "long_exchange": "binance", "short_exchange": "bitget"},
        {"symbol": "ETHUSDT", "long_exchange": "mexc", "short_exchange": "kucoin"},
    ]

    targets = build_depth_targets_from_candidates(candidates, max_candidates=2)

    assert targets == [("binance", "BTCUSDT"), ("bitget", "BTCUSDT")]


def test_candidate_watchlist_tracks_and_expires_routes():
    now = datetime.now(timezone.utc)
    watchlist = CandidateWatchlist(ttl_seconds=10, max_routes=10)
    candidate = {
        "symbol": "BTCUSDT",
        "long_exchange": "binance",
        "short_exchange": "bitget",
        "direction": "long_binance_short_bitget",
        "fast_spread_pct": 0.5,
    }

    watchlist.add_candidate(
        candidate,
        observed_at_utc=now,
        reason="paper_ready",
        priority_bonus=4.0,
    )

    metadata = watchlist.metadata_for(candidate, now=now + timedelta(seconds=5))

    assert len(watchlist) == 1
    assert candidate_route_key(candidate) == "BTCUSDT|binance|bitget|long_binance_short_bitget"
    assert metadata["watchlist_reason"] == "paper_ready"
    assert metadata["watchlist_priority_bonus"] == 4.0
    assert metadata["watchlist_best_spread_pct"] == 0.5
    assert watchlist.candidates(now=now + timedelta(seconds=11)) == []


def test_watchlist_depth_warm_candidates_include_recent_routes():
    watchlist = CandidateWatchlist(ttl_seconds=60, max_routes=10)
    current = [
        {
            "symbol": "BTCUSDT",
            "long_exchange": "binance",
            "short_exchange": "bitget",
            "direction": "long_binance_short_bitget",
            "fast_spread_pct": 0.5,
        }
    ]
    watched = {
        "symbol": "ETHUSDT",
        "long_exchange": "mexc",
        "short_exchange": "kucoin",
        "direction": "long_mexc_short_kucoin",
        "fast_spread_pct": 0.4,
    }
    watchlist.add_candidate(watched, reason="spread_ready", priority_bonus=2.0)

    warm_candidates = build_depth_warm_candidates(
        current,
        watchlist=watchlist,
        max_candidates=5,
    )

    assert {candidate_route_key(row) for row in warm_candidates} == {
        "BTCUSDT|binance|bitget|long_binance_short_bitget",
        "ETHUSDT|mexc|kucoin|long_mexc_short_kucoin",
    }


def test_deep_candidate_selection_prioritises_watchlisted_ready_route():
    watchlist = CandidateWatchlist(ttl_seconds=60, max_routes=10)
    ordinary = {
        "symbol": "BTCUSDT",
        "long_exchange": "binance",
        "short_exchange": "bitget",
        "direction": "long_binance_short_bitget",
        "fast_spread_pct": 0.8,
    }
    ready = {
        "symbol": "ETHUSDT",
        "long_exchange": "mexc",
        "short_exchange": "kucoin",
        "direction": "long_mexc_short_kucoin",
        "fast_spread_pct": 0.6,
    }
    watchlist.add_candidate(ready, reason="paper_ready", priority_bonus=4.0)

    selected = select_deep_candidates(
        [ordinary, ready],
        watchlist=watchlist,
        max_candidates=1,
    )

    assert selected == [ready]


def test_ticker_data_uses_cache_when_fresh_and_rest_when_cold():
    class DummyAdapter:
        def get_fast_futures_tickers(self):
            return {"ETHUSDT": {"exchange": "binance", "symbol": "ETHUSDT", "bid": 10, "ask": 11, "volume_usdt": 1}}

    cache = MarketDataCache()
    cache.update_ticker(
        CachedTicker(
            exchange="binance",
            symbol="BTCUSDT",
            bid=100.0,
            ask=101.0,
            volume_usdt=1_000_000.0,
            observed_at_utc=datetime.now(timezone.utc),
        )
    )

    ticker_data, source = get_ticker_data_with_cache(
        adapters={"binance": DummyAdapter()},
        cache=cache,
        max_age_seconds=10,
        min_cached_tickers=1,
    )
    assert source["binance"] == "websocket"
    assert set(ticker_data["binance"]) == {"BTCUSDT"}

    ticker_data, source = get_ticker_data_with_cache(
        adapters={"binance": DummyAdapter()},
        cache=cache,
        max_age_seconds=10,
        min_cached_tickers=2,
    )
    assert source["binance"] == "rest"
    assert set(ticker_data["binance"]) == {"ETHUSDT"}


def test_funding_info_cache_avoids_repeated_adapter_calls():
    class DummyFundingAdapter:
        def __init__(self):
            self.calls = 0

        def get_funding_info(self, symbol):
            self.calls += 1
            return FundingInfo(
                exchange="binance",
                standard_symbol=symbol,
                exchange_symbol=symbol,
                funding_rate=0.0001,
                next_funding_time_utc=None,
                funding_interval_hours=8,
                observed_at_utc=datetime.now(timezone.utc),
            )

    cache = MarketDataCache()
    adapter = DummyFundingAdapter()

    first = get_funding_info_with_cache(
        cache=cache,
        adapter=adapter,
        exchange="binance",
        symbol="BTCUSDT",
        max_age_seconds=60,
    )
    second = get_funding_info_with_cache(
        cache=cache,
        adapter=adapter,
        exchange="binance",
        symbol="BTCUSDT",
        max_age_seconds=60,
    )

    assert first.funding_rate == second.funding_rate
    assert adapter.calls == 1
    assert cache.stats().funding_counts == {"binance": 1}


def test_ticker_funding_fields_update_funding_cache():
    now = datetime.now(timezone.utc)
    next_funding = now + timedelta(hours=1)
    cache = MarketDataCache()

    cache.update_ticker(
        CachedTicker(
            exchange="bitget",
            symbol="BTCUSDT",
            bid=100.0,
            ask=101.0,
            volume_usdt=1_000_000.0,
            funding_rate=0.00012,
            next_funding_time_utc=next_funding,
            observed_at_utc=now,
        )
    )

    funding = cache.get_funding_info("bitget", "BTCUSDT", max_age_seconds=10)

    assert funding is not None
    assert funding.funding_rate == 0.00012
    assert funding.next_funding_time_utc == next_funding
    assert cache.stats().funding_counts == {"bitget": 1}


def test_hyperliquid_midpoint_rows_do_not_create_fast_candidates():
    ticker_data = {
        "binance": {
            "BTCUSDT": {
                "exchange": "binance",
                "symbol": "BTCUSDT",
                "bid": 100.0,
                "ask": 100.1,
                "volume_usdt": 1_000_000.0,
            }
        },
        "hyperliquid": {
            "BTCUSDT": {
                "exchange": "hyperliquid",
                "symbol": "BTCUSDT",
                "bid": 101.0,
                "ask": 101.0,
                "volume_usdt": 1_000_000.0,
                "price_source": "rest_mid",
            }
        },
    }

    assert build_fast_candidates(ticker_data) == []


def test_hyperliquid_bbo_rows_can_create_fast_candidates():
    ticker_data = {
        "binance": {
            "BTCUSDT": {
                "exchange": "binance",
                "symbol": "BTCUSDT",
                "bid": 100.0,
                "ask": 100.1,
                "volume_usdt": 1_000_000.0,
            }
        },
        "hyperliquid": {
            "BTCUSDT": {
                "exchange": "hyperliquid",
                "symbol": "BTCUSDT",
                "bid": 101.0,
                "ask": 101.1,
                "volume_usdt": 1_000_000.0,
                "price_source": "websocket_bbo",
            }
        },
    }

    candidates = build_fast_candidates(ticker_data)

    assert len(candidates) == 1
    assert candidates[0]["short_exchange"] == "hyperliquid"
    assert candidates[0]["short_price_source"] == "websocket_bbo"


def test_wait_for_candidate_orderbooks_reports_ready_routes():
    cache = MarketDataCache()
    now = datetime.now(timezone.utc)
    cache.update_orderbook(
        OrderBook(
            exchange="binance",
            market_type="futures",
            standard_symbol="BTCUSDT",
            exchange_symbol="BTCUSDT",
            bids=[OrderBookLevel(price=100.0, quantity=1.0)],
            asks=[OrderBookLevel(price=101.0, quantity=1.0)],
            observed_at_utc=now,
        )
    )
    cache.update_orderbook(
        OrderBook(
            exchange="bitget",
            market_type="futures",
            standard_symbol="BTCUSDT",
            exchange_symbol="BTCUSDT",
            bids=[OrderBookLevel(price=100.0, quantity=1.0)],
            asks=[OrderBookLevel(price=101.0, quantity=1.0)],
            observed_at_utc=now,
        )
    )
    candidates = [
        {"symbol": "BTCUSDT", "long_exchange": "binance", "short_exchange": "bitget"},
        {"symbol": "ETHUSDT", "long_exchange": "binance", "short_exchange": "bitget"},
    ]

    assert count_candidate_orderbook_coverage(
        candidates=candidates,
        cache=cache,
        max_age_seconds=10,
    ) == (1, 2)
    assert wait_for_candidate_orderbooks(
        candidates=candidates,
        cache=cache,
        timeout_seconds=0,
        poll_seconds=0.05,
        max_age_seconds=10,
    ) == (1, 2)


def test_deep_validation_uses_cached_books_and_funding():
    class DummyAdapter:
        def __init__(self, exchange):
            self.exchange = exchange
            self.orderbook_calls = 0
            self.funding_calls = 0

        def get_futures_orderbook(self, symbol, limit=100):
            self.orderbook_calls += 1
            raise AssertionError("REST orderbook should not be called when cache is fresh")

        def get_funding_info(self, symbol):
            self.funding_calls += 1
            raise AssertionError("REST funding should not be called when cache is fresh")

    now = datetime.now(timezone.utc)
    cache = MarketDataCache()
    cache.update_orderbook(
        OrderBook(
            exchange="binance",
            market_type="futures",
            standard_symbol="BTCUSDT",
            exchange_symbol="BTCUSDT",
            bids=[OrderBookLevel(price=99.0, quantity=100.0)],
            asks=[OrderBookLevel(price=100.0, quantity=100.0)],
            observed_at_utc=now,
        )
    )
    cache.update_orderbook(
        OrderBook(
            exchange="bitget",
            market_type="futures",
            standard_symbol="BTCUSDT",
            exchange_symbol="BTCUSDT",
            bids=[OrderBookLevel(price=101.0, quantity=100.0)],
            asks=[OrderBookLevel(price=102.0, quantity=100.0)],
            observed_at_utc=now,
        )
    )
    cache.update_funding_info(
        FundingInfo(
            exchange="binance",
            standard_symbol="BTCUSDT",
            exchange_symbol="BTCUSDT",
            funding_rate=0.0001,
            next_funding_time_utc=None,
            funding_interval_hours=8,
            observed_at_utc=now,
        )
    )
    cache.update_funding_info(
        FundingInfo(
            exchange="bitget",
            standard_symbol="BTCUSDT",
            exchange_symbol="BTCUSDT",
            funding_rate=0.0002,
            next_funding_time_utc=None,
            funding_interval_hours=8,
            observed_at_utc=now,
        )
    )

    adapters = {
        "binance": DummyAdapter("binance"),
        "bitget": DummyAdapter("bitget"),
    }
    rows = deep_validate_candidate(
        candidate={
            "symbol": "BTCUSDT",
            "instrument_class": "crypto",
            "long_exchange": "binance",
            "short_exchange": "bitget",
            "direction": "long_binance_short_bitget",
            "fast_spread_pct": 1.0,
            "long_ask": 100.0,
            "short_bid": 101.0,
            "combined_volume_usdt": 10_000_000.0,
        },
        adapters=adapters,
        timestamp=now,
        market_data_cache=cache,
        ws_orderbook_max_age_seconds=10,
        funding_cache_seconds=60,
    )

    assert rows
    assert all(row["long_fillable"] and row["short_fillable"] for row in rows)
    assert abs(rows[0]["validated_spread_pct"] - 1.0) < 1e-9
    assert adapters["binance"].orderbook_calls == 0
    assert adapters["bitget"].orderbook_calls == 0
    assert adapters["binance"].funding_calls == 0
    assert adapters["bitget"].funding_calls == 0


def test_parse_binance_book_ticker_and_mark_price():
    ticker = parse_binance_book_ticker(
        {
            "e": "bookTicker",
            "E": 1568014460893,
            "s": "BNBUSDT",
            "b": "25.35190000",
            "B": "31.21000000",
            "a": "25.36520000",
            "A": "40.66000000",
        }
    )
    assert ticker is not None
    assert ticker.exchange == "binance"
    assert ticker.symbol == "BNBUSDT"
    assert ticker.bid == 25.3519
    assert ticker.ask == 25.3652

    updates = parse_binance_mark_price_updates(
        [{"s": "BTCUSDT", "r": "0.00030000", "T": 1562306400000, "E": 1562305380000}]
    )
    assert updates[0][0] == "BTCUSDT"
    assert updates[0][1] == 0.0003
    assert updates[0][2] is not None


def test_parse_binance_depth():
    book = parse_binance_depth(
        {
            "E": 1571889248277,
            "s": "BTCUSDT",
            "b": [["7403.89", "0.002"]],
            "a": [["7405.96", "3.340"]],
        }
    )
    assert book is not None
    assert book.exchange == "binance"
    assert book.standard_symbol == "BTCUSDT"
    assert book.bids[0].price == 7403.89
    assert book.asks[0].quantity == 3.34


def test_parse_bitget_ticker_and_depth():
    tickers = parse_bitget_tickers(
        {
            "data": [
                {
                    "instId": "BTCUSDT",
                    "bidPr": "87673.6",
                    "askPr": "87673.7",
                    "quoteVolume": "1521198076.61216",
                    "fundingRate": "0.000055",
                    "nextFundingTime": "1766678400000",
                    "ts": "1766674540816",
                }
            ],
            "arg": {"channel": "ticker"},
        }
    )
    assert len(tickers) == 1
    assert tickers[0].exchange == "bitget"
    assert tickers[0].funding_rate == 0.000055
    assert tickers[0].next_funding_time_utc is not None

    book = parse_bitget_depth(
        {
            "arg": {"channel": "books5", "instId": "BTCUSDT"},
            "data": [{"bids": [["27000.0", "2.710"]], "asks": [["27000.5", "8.760"]], "ts": "1695716059516"}],
        }
    )
    assert book is not None
    assert book.exchange == "bitget"
    assert book.bids[0].price == 27000.0


def test_parse_mexc_ticker_funding_and_depth():
    tickers = parse_mexc_tickers(
        {
            "channel": "push.ticker",
            "data": {
                "ask1": 6866.5,
                "bid1": 6865,
                "fundingRate": 0.0008,
                "symbol": "BTC_USDT",
                "timestamp": 1587442022003,
                "volume24": 164586129,
            },
            "symbol": "BTC_USDT",
        }
    )
    assert len(tickers) == 1
    assert tickers[0].exchange == "mexc"
    assert tickers[0].symbol == "BTCUSDT"
    assert tickers[0].funding_rate == 0.0008
    assert parse_mexc_tickers(
        {
            "channel": "push.tickers",
            "data": [{"symbol": "BTC_USDT", "fairPrice": 65000.0}],
        }
    ) == []

    funding = parse_mexc_funding(
        {
            "channel": "push.funding.rate",
            "data": {"rate": 0.001, "symbol": "BTC_USDT", "nextSettleTime": 1587442022003},
            "symbol": "BTC_USDT",
        }
    )
    assert funding is not None
    assert funding[0] == "BTCUSDT"
    assert funding[1] == 0.001

    book = parse_mexc_depth(
        {
            "channel": "push.depth",
            "data": {"asks": [[6859.5, 3251, 1]], "bids": [[6858.5, 10, 1]]},
            "symbol": "BTC_USDT",
            "ts": 1587442022003,
        }
    )
    assert book is not None
    assert book.exchange == "mexc"
    assert book.asks[0].price == 6859.5
    assert book.asks[0].quantity == 1


def test_parse_kucoin_ticker_and_depth():
    ticker = parse_kucoin_ticker(
        {
            "type": "message",
            "topic": "/contractMarket/tickerV2:XBTUSDTM",
            "subject": "tickerV2",
            "data": {
                "symbol": "XBTUSDTM",
                "bestBidPrice": "65000.1",
                "bestAskPrice": "65000.2",
                "bestBidSize": "2.1",
                "bestAskSize": "1.9",
                "turnoverOf24h": "1000000",
                "ts": 1710000000000,
            },
        }
    )
    assert ticker is not None
    assert ticker.exchange == "kucoin"
    assert ticker.symbol == "BTCUSDT"
    assert ticker.bid == 65000.1
    assert ticker.ask_qty == 1.9

    book = parse_kucoin_depth(
        {
            "type": "message",
            "topic": "/contractMarket/level2Depth50:XBTUSDTM",
            "subject": "level2",
            "data": {
                "bids": [["64999.0", "1.2"]],
                "asks": [["65001.0", "1.1"]],
                "timestamp": 1710000000000,
            },
        }
    )
    assert book is not None
    assert book.exchange == "kucoin"
    assert book.standard_symbol == "BTCUSDT"
    assert book.bids[0].quantity == 1.2


def test_parse_hyperliquid_messages():
    mids = parse_hyperliquid_all_mids(
        {"channel": "allMids", "data": {"mids": {"BTC": "65000.0", "#1040": "0.1"}}}
    )
    assert len(mids) == 1
    assert mids[0].exchange == "hyperliquid"
    assert mids[0].symbol == "BTCUSDT"
    assert mids[0].bid == 65000.0
    assert mids[0].source == "websocket_mid"

    bbo = parse_hyperliquid_bbo(
        {
            "channel": "bbo",
            "data": {
                "coin": "BTC",
                "time": 1710000000000,
                "bbo": [{"px": "64999.0", "sz": "1.2"}, {"px": "65001.0", "sz": "1.1"}],
            },
        }
    )
    assert bbo is not None
    assert bbo.ask == 65001.0
    assert bbo.source == "websocket_bbo"

    funding = parse_hyperliquid_active_asset_ctx(
        {"channel": "activeAssetCtx", "data": {"coin": "BTC", "ctx": {"funding": "0.0001"}}}
    )
    assert funding is not None
    assert funding[0] == "BTCUSDT"
    assert funding[1] == 0.0001

    book = parse_hyperliquid_l2_book(
        {
            "channel": "l2Book",
            "data": {
                "coin": "BTC",
                "time": 1710000000000,
                "levels": [[{"px": "64999.0", "sz": "1.2"}], [{"px": "65001.0", "sz": "1.1"}]],
            },
        }
    )
    assert book is not None
    assert book.standard_symbol == "BTCUSDT"
    assert book.bids[0].quantity == 1.2


if __name__ == "__main__":
    test_cache_returns_only_fresh_usable_tickers()
    test_depth_targets_expire()
    test_build_depth_targets_from_candidates_limits_and_dedupes()
    test_ticker_data_uses_cache_when_fresh_and_rest_when_cold()
    test_funding_info_cache_avoids_repeated_adapter_calls()
    test_ticker_funding_fields_update_funding_cache()
    test_hyperliquid_midpoint_rows_do_not_create_fast_candidates()
    test_hyperliquid_bbo_rows_can_create_fast_candidates()
    test_wait_for_candidate_orderbooks_reports_ready_routes()
    test_deep_validation_uses_cached_books_and_funding()
    test_parse_binance_book_ticker_and_mark_price()
    test_parse_binance_depth()
    test_parse_bitget_ticker_and_depth()
    test_parse_mexc_ticker_funding_and_depth()
    test_parse_kucoin_ticker_and_depth()
    test_parse_hyperliquid_messages()
    print("market data websocket tests passed")
