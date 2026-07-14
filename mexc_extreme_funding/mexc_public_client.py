from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Optional

import requests

from mexc_extreme_funding.config import DEFAULT_CONFIG, MexcExtremeFundingConfig
from mexc_extreme_funding.models import FundingSnapshot, parse_float, utc_now


CONTRACT_URL = "https://contract.mexc.com"
SPOT_URL = "https://api.mexc.com"


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


class MexcPublicClient:
    def __init__(self, config: MexcExtremeFundingConfig = DEFAULT_CONFIG) -> None:
        self.config = config
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "mexc-extreme-funding-paper/1.0"})

    def _get(self, base_url: str, path: str, params: Optional[dict] = None) -> Any:
        response = self.session.get(
            f"{base_url}{path}",
            params=params,
            timeout=self.config.request_timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict) and payload.get("success") is False:
            raise ValueError(f"MEXC API error code={payload.get('code')} message={payload.get('message')}")
        if self.config.request_sleep_seconds:
            time.sleep(self.config.request_sleep_seconds)
        return payload

    def _funding_detail(self, symbol: str) -> dict:
        payload = self._get(CONTRACT_URL, f"/api/v1/contract/funding_rate/{symbol}")
        data = payload.get("data") if isinstance(payload, dict) else {}
        return data if isinstance(data, dict) else {}

    def fetch_snapshots(self, now: Optional[datetime] = None) -> list[FundingSnapshot]:
        observed = now or utc_now()
        ticker_payload = self._get(CONTRACT_URL, "/api/v1/contract/ticker")
        tickers = ticker_payload.get("data") if isinstance(ticker_payload, dict) else []
        if isinstance(tickers, dict):
            tickers = list(tickers.values())
        spot_payload = self._get(SPOT_URL, "/api/v3/ticker/bookTicker")
        spot_rows = spot_payload if isinstance(spot_payload, list) else []
        spot_lookup = {str(row.get("symbol")): row for row in spot_rows if isinstance(row, dict)}
        snapshots: list[FundingSnapshot] = []

        for row in tickers if isinstance(tickers, list) else []:
            if not isinstance(row, dict):
                continue
            symbol = str(row.get("symbol") or "")
            if not symbol.endswith("_USDT"):
                continue
            rate = parse_float(row.get("fundingRate"))
            rate_pct = None if rate is None else rate * 100
            detail: dict = {}
            if rate_pct is not None and abs(rate_pct) >= self.config.min_abs_funding_rate_pct:
                detail = self._funding_detail(symbol)
                detailed_rate = parse_float(detail.get("fundingRate"))
                if detailed_rate is not None:
                    rate_pct = detailed_rate * 100
            funding_time = _datetime_ms(detail.get("nextSettleTime"))
            minutes = None if funding_time is None else (funding_time - observed).total_seconds() / 60
            index_price = parse_float(row.get("indexPrice") or detail.get("indexPrice") or detail.get("idxPrice"))
            mark_price = parse_float(row.get("fairPrice") or detail.get("fairPrice"))
            perp_bid = parse_float(row.get("bid1") or row.get("bidPrice"))
            perp_ask = parse_float(row.get("ask1") or row.get("askPrice"))
            spot_symbol = symbol.replace("_", "")
            spot_book = spot_lookup.get(spot_symbol, {})
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
                    base=symbol[:-5],
                    spot_symbol=spot_symbol if spot_symbol in spot_lookup else "",
                    perp_symbol=symbol,
                    current_funding_rate_pct=rate_pct,
                    predicted_funding_rate_pct=rate_pct,
                    next_funding_time_utc=funding_time,
                    minutes_to_funding=minutes,
                    funding_interval_hours=parse_float(detail.get("collectCycle")),
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
        payload = self._get(
            CONTRACT_URL,
            "/api/v1/contract/funding_rate/history",
            params={"symbol": symbol, "page_num": 1, "page_size": 100},
        )
        data = payload.get("data") if isinstance(payload, dict) else {}
        rows = data.get("resultList") if isinstance(data, dict) else []
        target_ms = int(funding_time.timestamp() * 1000)
        nearest: tuple[int, float] | None = None
        for row in rows if isinstance(rows, list) else []:
            rate = parse_float(row.get("fundingRate"))
            try:
                diff = abs(int(row.get("settleTime")) - target_ms)
            except (TypeError, ValueError):
                continue
            if rate is not None and diff <= 120_000 and (nearest is None or diff < nearest[0]):
                nearest = (diff, rate * 100)
        return None if nearest is None else nearest[1]
