from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

import requests

from binance_extreme_funding.config import BinanceExtremeFundingConfig, DEFAULT_CONFIG
from binance_extreme_funding.models import FundingSnapshot, parse_float, utc_now
from core.models import OrderBook
from core.orderbook import parse_orderbook_levels


FUTURES_URL = "https://fapi.binance.com"
SPOT_URL = "https://api.binance.com"


def _mid(bid: Optional[float], ask: Optional[float]) -> Optional[float]:
    if bid is None or ask is None or bid <= 0 or ask <= 0:
        return None
    return (bid + ask) / 2


def _basis(reference: Optional[float], derivative: Optional[float]) -> Optional[float]:
    if reference is None or derivative is None or reference <= 0:
        return None
    return (derivative / reference - 1) * 100


def _entry_basis(
    rate_pct: Optional[float],
    spot_bid: Optional[float],
    spot_ask: Optional[float],
    perp_bid: Optional[float],
    perp_ask: Optional[float],
) -> Optional[float]:
    if rate_pct is None:
        return None
    if rate_pct > 0:
        return _basis(spot_ask, perp_bid)
    return _basis(spot_bid, perp_ask)


def _datetime_ms(value: Any) -> Optional[datetime]:
    try:
        return datetime.fromtimestamp(float(value) / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


class BinancePublicClient:
    def __init__(self, config: BinanceExtremeFundingConfig = DEFAULT_CONFIG) -> None:
        self.config = config
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "binance-extreme-funding-paper/1.0"})

    def _get(self, base_url: str, path: str, params: Optional[dict] = None) -> Any:
        response = self.session.get(
            f"{base_url}{path}",
            params=params,
            timeout=self.config.request_timeout_seconds,
        )
        response.raise_for_status()
        return response.json()

    def fetch_snapshots(self, now: Optional[datetime] = None) -> list[FundingSnapshot]:
        observed = now or utc_now()
        premium = self._get(FUTURES_URL, "/fapi/v1/premiumIndex")
        try:
            funding_info = self._get(FUTURES_URL, "/fapi/v1/fundingInfo")
        except requests.RequestException:
            funding_info = []
        futures_books = self._get(FUTURES_URL, "/fapi/v1/ticker/bookTicker")
        spot_books = self._get(SPOT_URL, "/api/v3/ticker/bookTicker")
        futures_lookup = {str(row.get("symbol")): row for row in futures_books if isinstance(row, dict)}
        spot_lookup = {str(row.get("symbol")): row for row in spot_books if isinstance(row, dict)}
        interval_lookup = {
            str(row.get("symbol")): parse_float(row.get("fundingIntervalHours"), 8.0) or 8.0
            for row in funding_info if isinstance(row, dict)
        }
        snapshots: list[FundingSnapshot] = []

        for row in premium if isinstance(premium, list) else []:
            if not isinstance(row, dict):
                continue
            symbol = str(row.get("symbol") or "")
            if not symbol.endswith("USDT"):
                continue
            rate = parse_float(row.get("lastFundingRate"))
            rate_pct = None if rate is None else rate * 100
            funding_time = _datetime_ms(row.get("nextFundingTime"))
            minutes = None if funding_time is None else (funding_time - observed).total_seconds() / 60
            index_price = parse_float(row.get("indexPrice"))
            mark_price = parse_float(row.get("markPrice"))
            futures_book = futures_lookup.get(symbol, {})
            spot_book = spot_lookup.get(symbol, {})
            perp_bid = parse_float(futures_book.get("bidPrice"))
            perp_ask = parse_float(futures_book.get("askPrice"))
            spot_bid = parse_float(spot_book.get("bidPrice"))
            spot_ask = parse_float(spot_book.get("askPrice"))
            spot_mid = _mid(spot_bid, spot_ask)
            perp_mid = _mid(perp_bid, perp_ask)
            executable_basis = _entry_basis(rate_pct, spot_bid, spot_ask, perp_bid, perp_ask)

            if rate_pct is None:
                reason = "funding_rate_missing"
            elif abs(rate_pct) < self.config.min_abs_funding_rate_pct:
                reason = "funding_below_threshold"
            elif funding_time is None:
                reason = "funding_time_missing"
            elif minutes is None or minutes < self.config.min_minutes_before_funding:
                reason = "too_close_to_funding"
            elif spot_mid is None or perp_mid is None:
                reason = "matching_spot_or_perp_book_missing"
            else:
                reason = "eligible"

            snapshots.append(
                FundingSnapshot(
                    observed_at_utc=observed,
                    exchange=self.config.exchange,
                    base=symbol[:-4],
                    spot_symbol=symbol if symbol in spot_lookup else "",
                    perp_symbol=symbol,
                    current_funding_rate_pct=rate_pct,
                    predicted_funding_rate_pct=rate_pct,
                    next_funding_time_utc=funding_time,
                    minutes_to_funding=minutes,
                    funding_interval_hours=interval_lookup.get(symbol, 8.0),
                    index_price=index_price,
                    mark_price=mark_price,
                    mark_index_basis_pct=_basis(index_price, mark_price),
                    spot_bid=spot_bid,
                    spot_ask=spot_ask,
                    perp_bid=perp_bid,
                    perp_ask=perp_ask,
                    executable_basis_pct=executable_basis,
                    eligible=reason == "eligible",
                    reason=reason,
                )
            )
        return snapshots

    def fetch_settled_rate(self, symbol: str, funding_time: datetime) -> Optional[float]:
        target_ms = int(funding_time.timestamp() * 1000)
        payload = self._get(
            FUTURES_URL,
            "/fapi/v1/fundingRate",
            params={"symbol": symbol, "startTime": target_ms - 120_000, "endTime": target_ms + 120_000, "limit": 10},
        )
        nearest: tuple[int, float] | None = None
        for row in payload if isinstance(payload, list) else []:
            rate = parse_float(row.get("fundingRate"))
            try:
                diff = abs(int(row.get("fundingTime")) - target_ms)
            except (TypeError, ValueError):
                continue
            if rate is not None and diff <= 120_000 and (nearest is None or diff < nearest[0]):
                nearest = (diff, rate * 100)
        return None if nearest is None else nearest[1]

    def fetch_orderbooks(
        self,
        spot_symbol: str,
        perp_symbol: str,
        observed_at: datetime,
        limit: int = 100,
    ) -> tuple[OrderBook, OrderBook]:
        spot = self._get(SPOT_URL, "/api/v3/depth", params={"symbol": spot_symbol, "limit": limit})
        spot_observed_at = utc_now()
        perp = self._get(FUTURES_URL, "/fapi/v1/depth", params={"symbol": perp_symbol, "limit": limit})
        perp_observed_at = utc_now()
        standard_symbol = perp_symbol
        return (
            OrderBook(
                exchange="binance", market_type="spot", standard_symbol=standard_symbol,
                exchange_symbol=spot_symbol,
                bids=parse_orderbook_levels(spot.get("bids", []), max_levels=limit),
                asks=parse_orderbook_levels(spot.get("asks", []), max_levels=limit),
                observed_at_utc=spot_observed_at,
            ),
            OrderBook(
                exchange="binance", market_type="futures", standard_symbol=standard_symbol,
                exchange_symbol=perp_symbol,
                bids=parse_orderbook_levels(perp.get("bids", []), max_levels=limit),
                asks=parse_orderbook_levels(perp.get("asks", []), max_levels=limit),
                observed_at_utc=perp_observed_at,
            ),
        )
