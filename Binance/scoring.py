# scoring.py

from config import TAKER_FEE_PCT


def safe_float(value, default=None):
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def calculate_orderbook_metrics(orderbook):
    bids = orderbook.get("bids", [])
    asks = orderbook.get("asks", [])

    if not bids or not asks:
        return None

    best_bid = safe_float(bids[0][0])
    best_ask = safe_float(asks[0][0])

    if best_bid is None or best_ask is None or best_bid <= 0 or best_ask <= 0:
        return None

    mid_price = (best_bid + best_ask) / 2
    spread_pct = ((best_ask - best_bid) / mid_price) * 100

    lower_bid_limit = mid_price * 0.99
    upper_ask_limit = mid_price * 1.01

    bid_depth_1pct = 0.0
    ask_depth_1pct = 0.0

    for price, size in bids:
        price = safe_float(price, 0)
        size = safe_float(size, 0)

        if price >= lower_bid_limit:
            bid_depth_1pct += price * size

    for price, size in asks:
        price = safe_float(price, 0)
        size = safe_float(size, 0)

        if price <= upper_ask_limit:
            ask_depth_1pct += price * size

    return {
        "best_bid": best_bid,
        "best_ask": best_ask,
        "mid_price": mid_price,
        "spread_pct": spread_pct,
        "bid_depth_1pct": bid_depth_1pct,
        "ask_depth_1pct": ask_depth_1pct,
    }


def calculate_liquidity_score(bid_depth_1pct, ask_depth_1pct):
    liquidity_depth = min(bid_depth_1pct or 0, ask_depth_1pct or 0)

    if liquidity_depth >= 500_000:
        return 100
    if liquidity_depth >= 250_000:
        return 75
    if liquidity_depth >= 100_000:
        return 50
    if liquidity_depth >= 25_000:
        return 25

    return 5


def calculate_opportunity_score(
    funding_rate,
    spread_pct,
    bid_depth_1pct,
    ask_depth_1pct
):
    funding_rate_pct = funding_rate * 100
    absolute_funding_rate_pct = abs(funding_rate_pct)

    estimated_fees_pct = TAKER_FEE_PCT * 2
    rough_net_edge_pct = absolute_funding_rate_pct - estimated_fees_pct - spread_pct

    liquidity_score = calculate_liquidity_score(
        bid_depth_1pct=bid_depth_1pct,
        ask_depth_1pct=ask_depth_1pct,
    )

    if rough_net_edge_pct <= 0:
        opportunity_score = 0
    else:
        edge_score = rough_net_edge_pct * 100
        opportunity_score = (edge_score * 0.7) + (liquidity_score * 0.3)

    return {
        "funding_rate_pct": funding_rate_pct,
        "absolute_funding_rate_pct": absolute_funding_rate_pct,
        "estimated_fees_pct": estimated_fees_pct,
        "rough_net_edge_pct": rough_net_edge_pct,
        "liquidity_score": liquidity_score,
        "opportunity_score": opportunity_score,
    }