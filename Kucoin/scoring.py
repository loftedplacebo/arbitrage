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


def calculate_opportunity_score(
    funding_rate,
    time_to_funding_minutes,
    spread_pct,
    bid_depth_1pct,
    ask_depth_1pct
):
    funding_rate_pct = funding_rate * 100
    absolute_funding_rate_pct = abs(funding_rate_pct)

    estimated_fees_pct = TAKER_FEE_PCT * 2
    rough_net_edge_pct = absolute_funding_rate_pct - estimated_fees_pct - spread_pct

    liquidity_depth = min(bid_depth_1pct or 0, ask_depth_1pct or 0)

    if liquidity_depth >= 250_000:
        liquidity_score = 100
    elif liquidity_depth >= 100_000:
        liquidity_score = 75
    elif liquidity_depth >= 50_000:
        liquidity_score = 50
    elif liquidity_depth >= 10_000:
        liquidity_score = 25
    else:
        liquidity_score = 5

    edge_score = max(0, rough_net_edge_pct * 100)

    # Time-to-funding deliberately removed from opportunity score.
    # The score is now based only on edge and liquidity.
    opportunity_score = (edge_score * 0.7) + (liquidity_score * 0.3)

    return {
        "funding_rate_pct": funding_rate_pct,
        "absolute_funding_rate_pct": absolute_funding_rate_pct,
        "estimated_fees_pct": estimated_fees_pct,
        "rough_net_edge_pct": rough_net_edge_pct,
        "liquidity_score": liquidity_score,
        "opportunity_score": opportunity_score,
    }