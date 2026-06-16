from __future__ import annotations

from datetime import datetime, timedelta, timezone

from market_data.cache import CachedTicker, MarketDataCache
from market_data.scanner_integration import (
    build_depth_targets_from_candidates,
    get_ticker_data_with_cache,
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


def test_build_depth_targets_from_candidates_limits_and_dedupes():
    candidates = [
        {"symbol": "BTCUSDT", "long_exchange": "binance", "short_exchange": "bitget"},
        {"symbol": "BTCUSDT", "long_exchange": "binance", "short_exchange": "bitget"},
        {"symbol": "ETHUSDT", "long_exchange": "mexc", "short_exchange": "kucoin"},
    ]

    targets = build_depth_targets_from_candidates(candidates, max_candidates=2)

    assert targets == [("binance", "BTCUSDT"), ("bitget", "BTCUSDT")]


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


def test_parse_hyperliquid_messages():
    mids = parse_hyperliquid_all_mids(
        {"channel": "allMids", "data": {"mids": {"BTC": "65000.0", "#1040": "0.1"}}}
    )
    assert len(mids) == 1
    assert mids[0].exchange == "hyperliquid"
    assert mids[0].symbol == "BTCUSDT"
    assert mids[0].bid == 65000.0

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
    test_parse_binance_book_ticker_and_mark_price()
    test_parse_binance_depth()
    test_parse_bitget_ticker_and_depth()
    test_parse_mexc_ticker_funding_and_depth()
    test_parse_hyperliquid_messages()
    print("market data websocket tests passed")
