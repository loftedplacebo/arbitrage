from __future__ import annotations

from typing import Optional


def pct_diff(numerator_price: float, denominator_price: float) -> float:
    if denominator_price <= 0:
        raise ValueError("denominator_price must be positive")
    return ((numerator_price / denominator_price) - 1) * 100


def calculate_spot_futures_basis_pct(
    spot_ask: float,
    futures_bid: float,
) -> float:
    """
    Positive means futures can be sold above spot purchase price.

    Trade:
        Buy spot at spot ask.
        Short futures at futures bid.
    """
    return pct_diff(futures_bid, spot_ask)


def calculate_futures_futures_spread_pct(
    long_ask: float,
    short_bid: float,
) -> float:
    """
    Positive means short venue futures are priced above long venue futures.

    Trade:
        Long lower-priced futures at ask.
        Short higher-priced futures at bid.
    """
    return pct_diff(short_bid, long_ask)


def calculate_funding_diff_pct(
    long_funding_rate: Optional[float],
    short_funding_rate: Optional[float],
) -> Optional[float]:
    """
    For futures-futures:
        Long leg receives funding when funding is negative.
        Short leg receives funding when funding is positive.

    Approx funding benefit:
        short_funding_rate - long_funding_rate
    """
    if long_funding_rate is None or short_funding_rate is None:
        return None

    return (short_funding_rate - long_funding_rate) * 100


def calculate_net_edge_pct(
    gross_spread_pct: float,
    estimated_fees_pct: float,
    estimated_slippage_pct: float,
    expected_funding_pct: float = 0.0,
) -> float:
    return gross_spread_pct + expected_funding_pct - estimated_fees_pct - estimated_slippage_pct


def classify_opportunity(net_edge_pct: float) -> str:
    if net_edge_pct >= 0.50:
        return "EXCELLENT"
    if net_edge_pct >= 0.25:
        return "STRONG"
    if net_edge_pct >= 0.10:
        return "WATCH"
    if net_edge_pct > 0:
        return "WEAK"
    return "NO_EDGE"