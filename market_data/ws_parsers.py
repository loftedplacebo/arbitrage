from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from core.models import OrderBook
from core.orderbook import parse_orderbook_levels
from core.symbols import normalise_symbol
from market_data.cache import CachedTicker


def parse_float(value: Any, default: float | None = None) -> float | None:
    if value in (None, ""):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def millis_to_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def observed_time_from_millis(value: Any) -> datetime:
    return millis_to_datetime(value) or datetime.now(timezone.utc)


def next_hour_utc(now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    return now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)


def parse_binance_book_ticker(payload: dict) -> CachedTicker | None:
    symbol = payload.get("s")
    bid = parse_float(payload.get("b"))
    ask = parse_float(payload.get("a"))
    if not symbol or bid is None or ask is None or bid <= 0 or ask <= 0:
        return None
    return CachedTicker(
        exchange="binance",
        symbol=normalise_symbol(symbol),
        bid=bid,
        ask=ask,
        volume_usdt=0.0,
        bid_qty=parse_float(payload.get("B")),
        ask_qty=parse_float(payload.get("A")),
        observed_at_utc=observed_time_from_millis(payload.get("E") or payload.get("T")),
    )


def parse_binance_mark_price_updates(payload: Any) -> list[tuple[str, float | None, datetime | None, datetime]]:
    updates = payload if isinstance(payload, list) else [payload]
    parsed = []
    for item in updates:
        if not isinstance(item, dict):
            continue
        symbol = item.get("s")
        if not symbol:
            continue
        parsed.append(
            (
                normalise_symbol(symbol),
                parse_float(item.get("r")),
                millis_to_datetime(item.get("T")),
                observed_time_from_millis(item.get("E")),
            )
        )
    return parsed


def parse_binance_depth(payload: dict) -> OrderBook | None:
    symbol = payload.get("s")
    if not symbol:
        return None
    return OrderBook(
        exchange="binance",
        market_type="futures",
        standard_symbol=normalise_symbol(symbol),
        exchange_symbol=str(symbol),
        bids=parse_orderbook_levels(payload.get("b") or [], max_levels=100),
        asks=parse_orderbook_levels(payload.get("a") or [], max_levels=100),
        observed_at_utc=observed_time_from_millis(payload.get("E") or payload.get("T")),
    )


def parse_bitget_tickers(payload: dict) -> list[CachedTicker]:
    rows = payload.get("data") or []
    if isinstance(rows, dict):
        rows = [rows]
    tickers = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        symbol = item.get("instId") or item.get("symbol")
        bid = parse_float(item.get("bidPr"))
        ask = parse_float(item.get("askPr"))
        if not symbol or bid is None or ask is None or bid <= 0 or ask <= 0:
            continue
        tickers.append(
            CachedTicker(
                exchange="bitget",
                symbol=normalise_symbol(symbol),
                bid=bid,
                ask=ask,
                volume_usdt=parse_float(item.get("quoteVolume") or item.get("usdtVolume"), 0.0) or 0.0,
                bid_qty=parse_float(item.get("bidSz")),
                ask_qty=parse_float(item.get("askSz")),
                funding_rate=parse_float(item.get("fundingRate")),
                next_funding_time_utc=millis_to_datetime(item.get("nextFundingTime")),
                observed_at_utc=observed_time_from_millis(item.get("ts") or payload.get("ts")),
            )
        )
    return tickers


def parse_bitget_depth(payload: dict) -> OrderBook | None:
    arg = payload.get("arg") or {}
    symbol = arg.get("instId")
    data = payload.get("data") or []
    item = data[0] if isinstance(data, list) and data else {}
    if not symbol or not isinstance(item, dict):
        return None
    return OrderBook(
        exchange="bitget",
        market_type="futures",
        standard_symbol=normalise_symbol(symbol),
        exchange_symbol=str(symbol),
        bids=parse_orderbook_levels(item.get("bids") or [], max_levels=100),
        asks=parse_orderbook_levels(item.get("asks") or [], max_levels=100),
        observed_at_utc=observed_time_from_millis(item.get("ts") or payload.get("ts")),
    )


def parse_mexc_tickers(payload: dict) -> list[CachedTicker]:
    channel = payload.get("channel")
    if channel == "push.tickers":
        rows = payload.get("data") or []
    elif channel == "push.ticker":
        rows = [payload.get("data") or {}]
    else:
        return []

    tickers = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        symbol = item.get("symbol") or payload.get("symbol")
        bid = parse_float(item.get("bid1") or item.get("bidPrice"))
        ask = parse_float(item.get("ask1") or item.get("askPrice"))
        if not symbol:
            continue
        if bid is None or ask is None or bid <= 0 or ask <= 0:
            continue
        tickers.append(
            CachedTicker(
                exchange="mexc",
                symbol=normalise_symbol(symbol),
                bid=bid,
                ask=ask,
                volume_usdt=parse_float(
                    item.get("amount24") or item.get("volume24") or item.get("quoteVolume"),
                    0.0,
                )
                or 0.0,
                funding_rate=parse_float(item.get("fundingRate")),
                observed_at_utc=observed_time_from_millis(item.get("timestamp") or payload.get("ts")),
            )
        )
    return tickers


def parse_mexc_funding(payload: dict) -> tuple[str, float | None, datetime | None, datetime] | None:
    if payload.get("channel") != "push.funding.rate":
        return None
    item = payload.get("data") or {}
    symbol = item.get("symbol") or payload.get("symbol")
    if not symbol:
        return None
    return (
        normalise_symbol(symbol),
        parse_float(item.get("rate") or item.get("fundingRate")),
        millis_to_datetime(item.get("nextSettleTime")),
        observed_time_from_millis(payload.get("ts")),
    )


def parse_mexc_depth(payload: dict) -> OrderBook | None:
    if payload.get("channel") not in {"push.depth", "push.depth.full"}:
        return None
    symbol = payload.get("symbol") or (payload.get("data") or {}).get("symbol")
    item = payload.get("data") or {}
    if not symbol:
        return None
    return OrderBook(
        exchange="mexc",
        market_type="futures",
        standard_symbol=normalise_symbol(symbol),
        exchange_symbol=str(symbol),
        bids=parse_orderbook_levels(_normalise_mexc_depth_levels(item.get("bids") or []), max_levels=100),
        asks=parse_orderbook_levels(_normalise_mexc_depth_levels(item.get("asks") or []), max_levels=100),
        observed_at_utc=observed_time_from_millis(payload.get("ts")),
    )


def _normalise_mexc_depth_levels(levels: list) -> list:
    normalised = []
    for level in levels:
        if isinstance(level, (list, tuple)) and len(level) >= 3:
            normalised.append([level[0], level[2]])
        else:
            normalised.append(level)
    return normalised


def parse_hyperliquid_all_mids(payload: dict) -> list[CachedTicker]:
    if payload.get("channel") != "allMids":
        return []
    mids = (payload.get("data") or {}).get("mids") or {}
    tickers = []
    observed_at = datetime.now(timezone.utc)
    for coin, mid_raw in mids.items():
        if not _is_supported_hyperliquid_coin(str(coin)):
            continue
        mid = parse_float(mid_raw)
        if mid is None or mid <= 0:
            continue
        tickers.append(
            CachedTicker(
                exchange="hyperliquid",
                symbol=f"{str(coin).upper()}USDT",
                bid=mid,
                ask=mid,
                volume_usdt=0.0,
                observed_at_utc=observed_at,
            )
        )
    return tickers


def parse_hyperliquid_bbo(payload: dict) -> CachedTicker | None:
    if payload.get("channel") != "bbo":
        return None
    data = payload.get("data") or {}
    coin = data.get("coin")
    if coin and not _is_supported_hyperliquid_coin(str(coin)):
        return None
    bbo = data.get("bbo") or []
    if not coin or len(bbo) < 2 or bbo[0] is None or bbo[1] is None:
        return None
    bid = parse_float(bbo[0].get("px"))
    ask = parse_float(bbo[1].get("px"))
    if bid is None or ask is None or bid <= 0 or ask <= 0:
        return None
    return CachedTicker(
        exchange="hyperliquid",
        symbol=f"{str(coin).upper()}USDT",
        bid=bid,
        ask=ask,
        volume_usdt=0.0,
        bid_qty=parse_float(bbo[0].get("sz")),
        ask_qty=parse_float(bbo[1].get("sz")),
        observed_at_utc=observed_time_from_millis(data.get("time")),
    )


def parse_hyperliquid_active_asset_ctx(payload: dict) -> tuple[str, float | None, datetime | None, datetime] | None:
    if payload.get("channel") != "activeAssetCtx":
        return None
    data = payload.get("data") or {}
    coin = data.get("coin")
    if coin and not _is_supported_hyperliquid_coin(str(coin)):
        return None
    ctx = data.get("ctx") or {}
    if not coin:
        return None
    return (
        f"{str(coin).upper()}USDT",
        parse_float(ctx.get("funding")),
        next_hour_utc(),
        datetime.now(timezone.utc),
    )


def parse_hyperliquid_l2_book(payload: dict) -> OrderBook | None:
    if payload.get("channel") != "l2Book":
        return None
    data = payload.get("data") or {}
    coin = data.get("coin")
    if coin and not _is_supported_hyperliquid_coin(str(coin)):
        return None
    levels = data.get("levels") or []
    if not coin or len(levels) < 2:
        return None
    return OrderBook(
        exchange="hyperliquid",
        market_type="futures",
        standard_symbol=f"{str(coin).upper()}USDT",
        exchange_symbol=str(coin),
        bids=parse_orderbook_levels(levels[0], max_levels=100),
        asks=parse_orderbook_levels(levels[1], max_levels=100),
        observed_at_utc=observed_time_from_millis(data.get("time")),
    )


def _is_supported_hyperliquid_coin(coin: str) -> bool:
    return bool(coin) and coin.isalnum() and ":" not in coin and not coin.startswith("@")
