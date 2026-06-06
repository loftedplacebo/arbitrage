# compare_spread.py

import sqlite3
import time
from pathlib import Path
from datetime import datetime, timezone

import requests


# ------------------------------------------------------------
# Paths / config
# ------------------------------------------------------------

ARBITRAGE_DIR = Path(__file__).resolve().parents[1]

KUCOIN_DB_PATH = ARBITRAGE_DIR / "Kucoin" / "data" / "kucoin_scanner.db"
BINANCE_DB_PATH = ARBITRAGE_DIR / "Binance" / "data" / "binance_scanner.db"

SCAN_INTERVAL_SECONDS = 60

KUCOIN_FUTURES_BASE_URL = "https://api-futures.kucoin.com"
BINANCE_FUTURES_BASE_URL = "https://fapi.binance.com"

MAX_DATA_AGE_MINUTES = 5

# Keep this modest. KuCoin order books are slower than Binance.
MAX_SYMBOLS_TO_CHECK = 30

# Initial filters
MIN_ABS_MARK_SPREAD_PCT = 0.05

# Rough full round-trip fees across both exchanges:
# Open KuCoin + open Binance + close KuCoin + close Binance
# KuCoin taker open/close = 0.12%
# Binance taker open/close = 0.10%
# Total = 0.22%
ROUND_TRIP_BOTH_EXCHANGES_FEES_PCT = 0.22

TEST_NOTIONALS_USDT = [100, 500, 1000, 2500, 5000]

# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------

def utc_now():
    return datetime.now(timezone.utc)


def parse_timestamp(value):
    if value is None:
        return None

    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def minutes_since(timestamp_value):
    dt = parse_timestamp(timestamp_value)

    if dt is None:
        return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return (utc_now() - dt).total_seconds() / 60


def safe_float(value, default=None):
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def normalise_symbol(exchange, symbol):
    if not symbol:
        return None

    symbol = symbol.upper()

    # KuCoin futures: BTCUSDTM -> BTCUSDT
    if exchange == "kucoin" and symbol.endswith("USDTM"):
        return symbol[:-1]

    return symbol


# ------------------------------------------------------------
# Database reads
# ------------------------------------------------------------

def get_latest_funding_rows(db_path, exchange):
    """
    We use funding_snapshots only because it already contains latest mark/index prices.
    This script is spread-focused, but the existing table is enough to get symbol universe.
    """
    if not db_path.exists():
        raise FileNotFoundError(f"{exchange} database not found: {db_path}")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("""
        SELECT fs.*
        FROM funding_snapshots fs
        INNER JOIN (
            SELECT symbol, MAX(id) AS max_id
            FROM funding_snapshots
            GROUP BY symbol
        ) latest
            ON fs.symbol = latest.symbol
           AND fs.id = latest.max_id
    """)

    rows = [dict(row) for row in cur.fetchall()]
    conn.close()

    output = {}

    for row in rows:
        raw_symbol = row.get("symbol")
        common_symbol = normalise_symbol(exchange, raw_symbol)

        if not common_symbol:
            continue

        output[common_symbol] = {
            "exchange": exchange,
            "raw_symbol": raw_symbol,
            "common_symbol": common_symbol,
            "timestamp": row.get("timestamp"),
            "age_minutes": minutes_since(row.get("timestamp")),
            "mark_price": safe_float(row.get("mark_price")),
            "index_price": safe_float(row.get("index_price")),
            "funding_rate": safe_float(row.get("funding_rate")),
        }

    return output


# ------------------------------------------------------------
# Order book calls
# ------------------------------------------------------------

def get_kucoin_orderbook(symbol):
    url = f"{KUCOIN_FUTURES_BASE_URL}/api/v1/level2/snapshot"

    response = requests.get(
        url,
        params={"symbol": symbol},
        timeout=8,
    )
    response.raise_for_status()

    payload = response.json()

    if payload.get("code") != "200000":
        raise RuntimeError(f"KuCoin orderbook error for {symbol}: {payload}")

    return payload.get("data", {})


def get_binance_orderbook(symbol, limit=100):
    url = f"{BINANCE_FUTURES_BASE_URL}/fapi/v1/depth"

    response = requests.get(
        url,
        params={"symbol": symbol, "limit": limit},
        timeout=8,
    )
    response.raise_for_status()

    return response.json()


def get_top_of_book(orderbook):
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

    return {
        "best_bid": best_bid,
        "best_ask": best_ask,
        "mid_price": mid_price,
        "spread_pct": spread_pct,
    }


def estimate_depth_within_1pct(orderbook):
    bids = orderbook.get("bids", [])
    asks = orderbook.get("asks", [])

    if not bids or not asks:
        return {
            "bid_depth_1pct": 0,
            "ask_depth_1pct": 0,
            "depth_score": 0,
        }

    best_bid = safe_float(bids[0][0])
    best_ask = safe_float(asks[0][0])

    if not best_bid or not best_ask:
        return {
            "bid_depth_1pct": 0,
            "ask_depth_1pct": 0,
            "depth_score": 0,
        }

    mid_price = (best_bid + best_ask) / 2

    lower_bid_limit = mid_price * 0.99
    upper_ask_limit = mid_price * 1.01

    bid_depth = 0.0
    ask_depth = 0.0

    for price, size in bids:
        price = safe_float(price, 0)
        size = safe_float(size, 0)

        if price >= lower_bid_limit:
            bid_depth += price * size

    for price, size in asks:
        price = safe_float(price, 0)
        size = safe_float(size, 0)

        if price <= upper_ask_limit:
            ask_depth += price * size

    weakest_depth = min(bid_depth, ask_depth)

    if weakest_depth >= 500_000:
        depth_score = 100
    elif weakest_depth >= 250_000:
        depth_score = 75
    elif weakest_depth >= 100_000:
        depth_score = 50
    elif weakest_depth >= 25_000:
        depth_score = 25
    else:
        depth_score = 5

    return {
        "bid_depth_1pct": bid_depth,
        "ask_depth_1pct": ask_depth,
        "depth_score": depth_score,
    }


# ------------------------------------------------------------
# Spread logic
# ------------------------------------------------------------

def build_candidate_rows(kucoin_rows, binance_rows):
    common_symbols = sorted(set(kucoin_rows.keys()) & set(binance_rows.keys()))
    rows = []

    for symbol in common_symbols:
        k = kucoin_rows[symbol]
        b = binance_rows[symbol]

        kucoin_mark = k["mark_price"]
        binance_mark = b["mark_price"]

        if not kucoin_mark or not binance_mark:
            continue

        kucoin_age = k["age_minutes"]
        binance_age = b["age_minutes"]

        max_age = max(
            kucoin_age if kucoin_age is not None else 999,
            binance_age if binance_age is not None else 999,
        )

        if max_age > MAX_DATA_AGE_MINUTES:
            continue

        mark_spread_pct = ((kucoin_mark - binance_mark) / binance_mark) * 100

        if abs(mark_spread_pct) < MIN_ABS_MARK_SPREAD_PCT:
            continue

        rows.append({
            "symbol": symbol,
            "kucoin_symbol": k["raw_symbol"],
            "binance_symbol": b["raw_symbol"],
            "kucoin_mark": kucoin_mark,
            "binance_mark": binance_mark,
            "mark_spread_pct": mark_spread_pct,
            "kucoin_age_minutes": kucoin_age,
            "binance_age_minutes": binance_age,
            "kucoin_funding_rate": k["funding_rate"],
            "binance_funding_rate": b["funding_rate"],
        })

    rows = sorted(
        rows,
        key=lambda x: abs(x["mark_spread_pct"]),
        reverse=True
    )

    return rows
def simulate_market_buy(orderbook, target_notional_usdt):
    """
    Walks the ask side to simulate buying target notional.
    Returns average fill price, filled notional and whether fully filled.
    """
    asks = orderbook.get("asks", [])

    remaining_notional = target_notional_usdt
    filled_notional = 0.0
    filled_size = 0.0

    for price_raw, size_raw in asks:
        price = safe_float(price_raw, 0)
        size = safe_float(size_raw, 0)

        if price <= 0 or size <= 0:
            continue

        level_notional = price * size
        take_notional = min(remaining_notional, level_notional)
        take_size = take_notional / price

        filled_notional += take_notional
        filled_size += take_size
        remaining_notional -= take_notional

        if remaining_notional <= 0:
            break

    if filled_size <= 0:
        return {
            "avg_price": None,
            "filled_notional": filled_notional,
            "fully_filled": False,
        }

    avg_price = filled_notional / filled_size

    return {
        "avg_price": avg_price,
        "filled_notional": filled_notional,
        "fully_filled": filled_notional >= target_notional_usdt * 0.999,
    }


def simulate_market_sell(orderbook, target_notional_usdt):
    """
    Walks the bid side to simulate selling/shorting target notional.
    Returns average fill price, filled notional and whether fully filled.
    """
    bids = orderbook.get("bids", [])

    remaining_notional = target_notional_usdt
    filled_notional = 0.0
    filled_size = 0.0

    for price_raw, size_raw in bids:
        price = safe_float(price_raw, 0)
        size = safe_float(size_raw, 0)

        if price <= 0 or size <= 0:
            continue

        level_notional = price * size
        take_notional = min(remaining_notional, level_notional)
        take_size = take_notional / price

        filled_notional += take_notional
        filled_size += take_size
        remaining_notional -= take_notional

        if remaining_notional <= 0:
            break

    if filled_size <= 0:
        return {
            "avg_price": None,
            "filled_notional": filled_notional,
            "fully_filled": False,
        }

    avg_price = filled_notional / filled_size

    return {
        "avg_price": avg_price,
        "filled_notional": filled_notional,
        "fully_filled": filled_notional >= target_notional_usdt * 0.999,
    }


def calculate_executable_spread_pct(long_avg_price, short_avg_price):
    """
    Positive means favourable: we buy lower than we short.
    """
    if not long_avg_price or not short_avg_price:
        return None

    reference_price = (long_avg_price + short_avg_price) / 2

    if reference_price <= 0:
        return None

    return ((short_avg_price - long_avg_price) / reference_price) * 100


def simulate_direction_for_notional(
    long_orderbook,
    short_orderbook,
    target_notional_usdt
):
    """
    Simulates opening one market-neutral spread:
    - buy/long on long_orderbook asks
    - sell/short on short_orderbook bids
    """
    buy_result = simulate_market_buy(
        orderbook=long_orderbook,
        target_notional_usdt=target_notional_usdt,
    )

    sell_result = simulate_market_sell(
        orderbook=short_orderbook,
        target_notional_usdt=target_notional_usdt,
    )

    if not buy_result["fully_filled"] or not sell_result["fully_filled"]:
        return {
            "target_notional": target_notional_usdt,
            "fully_filled": False,
            "spread_pct": None,
            "net_after_fees_pct": None,
            "buy_avg_price": buy_result["avg_price"],
            "sell_avg_price": sell_result["avg_price"],
            "buy_filled_notional": buy_result["filled_notional"],
            "sell_filled_notional": sell_result["filled_notional"],
        }

    spread_pct = calculate_executable_spread_pct(
        long_avg_price=buy_result["avg_price"],
        short_avg_price=sell_result["avg_price"],
    )

    net_after_fees_pct = (
        spread_pct - ROUND_TRIP_BOTH_EXCHANGES_FEES_PCT
        if spread_pct is not None
        else None
    )

    return {
        "target_notional": target_notional_usdt,
        "fully_filled": True,
        "spread_pct": spread_pct,
        "net_after_fees_pct": net_after_fees_pct,
        "buy_avg_price": buy_result["avg_price"],
        "sell_avg_price": sell_result["avg_price"],
        "buy_filled_notional": buy_result["filled_notional"],
        "sell_filled_notional": sell_result["filled_notional"],
    }


def simulate_both_directions_by_notional(kucoin_book, binance_book):
    """
    For each test notional, simulate:
    A) Buy KuCoin / Short Binance
    B) Buy Binance / Short KuCoin
    Then keep the better direction.
    """
    results = {}

    for notional in TEST_NOTIONALS_USDT:
        route_a = simulate_direction_for_notional(
            long_orderbook=kucoin_book,
            short_orderbook=binance_book,
            target_notional_usdt=notional,
        )

        route_b = simulate_direction_for_notional(
            long_orderbook=binance_book,
            short_orderbook=kucoin_book,
            target_notional_usdt=notional,
        )

        route_a_net = (
            route_a["net_after_fees_pct"]
            if route_a["net_after_fees_pct"] is not None
            else -999
        )

        route_b_net = (
            route_b["net_after_fees_pct"]
            if route_b["net_after_fees_pct"] is not None
            else -999
        )

        if route_a_net >= route_b_net:
            best = route_a
            best["direction"] = "Buy KuCoin / Short Binance"
        else:
            best = route_b
            best["direction"] = "Buy Binance / Short KuCoin"

        results[notional] = best

    return results

def add_live_spread_check(row):
    kucoin_book = get_kucoin_orderbook(row["kucoin_symbol"])
    binance_book = get_binance_orderbook(row["binance_symbol"])

    kucoin_tob = get_top_of_book(kucoin_book)
    binance_tob = get_top_of_book(binance_book)

    if kucoin_tob is None or binance_tob is None:
        row["execution_status"] = "NO_BOOK"
        return row

    kucoin_depth = estimate_depth_within_1pct(kucoin_book)
    binance_depth = estimate_depth_within_1pct(binance_book)

    # Route A:
    # Buy KuCoin at ask, short Binance at bid.
    buy_kucoin_price = kucoin_tob["best_ask"]
    short_binance_price = binance_tob["best_bid"]
    ref_a = (buy_kucoin_price + short_binance_price) / 2
    buy_kucoin_short_binance_spread_pct = (
        (short_binance_price - buy_kucoin_price) / ref_a
    ) * 100

    # Route B:
    # Buy Binance at ask, short KuCoin at bid.
    buy_binance_price = binance_tob["best_ask"]
    short_kucoin_price = kucoin_tob["best_bid"]
    ref_b = (buy_binance_price + short_kucoin_price) / 2
    buy_binance_short_kucoin_spread_pct = (
        (short_kucoin_price - buy_binance_price) / ref_b
    ) * 100

    if buy_kucoin_short_binance_spread_pct >= buy_binance_short_kucoin_spread_pct:
        best_direction = "Buy KuCoin / Short Binance"
        best_spread_pct = buy_kucoin_short_binance_spread_pct
        long_exchange = "KuCoin"
        short_exchange = "Binance"
        long_price = buy_kucoin_price
        short_price = short_binance_price
    else:
        best_direction = "Buy Binance / Short KuCoin"
        best_spread_pct = buy_binance_short_kucoin_spread_pct
        long_exchange = "Binance"
        short_exchange = "KuCoin"
        long_price = buy_binance_price
        short_price = short_kucoin_price

    rough_net_after_fees_pct = best_spread_pct - ROUND_TRIP_BOTH_EXCHANGES_FEES_PCT

    notional_simulation = simulate_both_directions_by_notional(
        kucoin_book=kucoin_book,
        binance_book=binance_book,
    )
    row.update({
        "execution_status": "OK",
        "notional_simulation": notional_simulation,
        "kucoin_bid": kucoin_tob["best_bid"],
        "kucoin_ask": kucoin_tob["best_ask"],
        "binance_bid": binance_tob["best_bid"],
        "binance_ask": binance_tob["best_ask"],
        "kucoin_book_spread_pct": kucoin_tob["spread_pct"],
        "binance_book_spread_pct": binance_tob["spread_pct"],
        "buy_kucoin_short_binance_spread_pct": buy_kucoin_short_binance_spread_pct,
        "buy_binance_short_kucoin_spread_pct": buy_binance_short_kucoin_spread_pct,
        "best_direction": best_direction,
        "best_spread_pct": best_spread_pct,
        "rough_net_after_fees_pct": rough_net_after_fees_pct,
        "long_exchange": long_exchange,
        "short_exchange": short_exchange,
        "long_price": long_price,
        "short_price": short_price,
        "kucoin_depth_score": kucoin_depth["depth_score"],
        "binance_depth_score": binance_depth["depth_score"],
        "combined_depth_score": min(
            kucoin_depth["depth_score"],
            binance_depth["depth_score"],
        ),
    })

    return row


# ------------------------------------------------------------
# Printing
# ------------------------------------------------------------
def print_notional_simulation_table(rows):
    print("\nNotional-based executable spread simulation")
    print(
        "Symbol       "
        "Best Direction              "
        "Net $100   "
        "Net $500   "
        "Net $1k    "
        "Net $2.5k  "
        "Net $5k"
    )

    if not rows:
        print("No rows checked.")
        return

    rows = [
        row for row in rows
        if row.get("execution_status") == "OK"
    ]

    rows = sorted(
        rows,
        key=lambda x: (
            x.get("notional_simulation", {})
             .get(TEST_NOTIONALS_USDT[0], {})
             .get("net_after_fees_pct")
            if x.get("notional_simulation", {})
             .get(TEST_NOTIONALS_USDT[0], {})
             .get("net_after_fees_pct") is not None
            else -999
        ),
        reverse=True
    )

    for row in rows:
        simulations = row.get("notional_simulation", {})

        # Use the best direction for the smallest notional as the display direction.
        first_notional = TEST_NOTIONALS_USDT[0]
        first_result = simulations.get(first_notional, {})
        direction = first_result.get("direction", row.get("best_direction", ""))

        values = []

        for notional in TEST_NOTIONALS_USDT:
            result = simulations.get(notional, {})

            if not result.get("fully_filled"):
                values.append("NOFILL")
                continue

            net = result.get("net_after_fees_pct")

            if net is None:
                values.append("None")
            else:
                values.append(f"{net:.4f}%")

        print(
            f"{row['symbol']:<12}"
            f"{direction:<28}"
            f"{values[0]:>9}   "
            f"{values[1]:>9}   "
            f"{values[2]:>9}   "
            f"{values[3]:>9}   "
            f"{values[4]:>9}"
        )


def print_summary(kucoin_rows, binance_rows, candidate_rows, checked_rows):
    print("\nCross-exchange spread scanner")
    print("-----------------------------")
    print(f"KuCoin DB:               {KUCOIN_DB_PATH}")
    print(f"Binance DB:              {BINANCE_DB_PATH}")
    print(f"Fresh KuCoin symbols:    {len(kucoin_rows)}")
    print(f"Fresh Binance symbols:   {len(binance_rows)}")
    print(f"Spread candidates:       {len(candidate_rows)}")
    print(f"Live order books checked:{len(checked_rows)}")


def print_mark_spread_table(rows):
    print("\nTop mark-price spreads before live book check")
    print(
        "Symbol       "
        "KuCoin Mark   "
        "Binance Mark   "
        "Mark Spr %   "
        "K Age   "
        "B Age"
    )

    for row in rows[:20]:
        print(
            f"{row['symbol']:<12}"
            f"{row['kucoin_mark']:>11.6f}   "
            f"{row['binance_mark']:>12.6f}   "
            f"{row['mark_spread_pct']:>10.4f}   "
            f"{(row['kucoin_age_minutes'] or 0):>5.1f}   "
            f"{(row['binance_age_minutes'] or 0):>5.1f}"
        )


def print_live_spread_table(rows):
    print("\nLive executable spread check")
    print(
        "Symbol       "
        "Best Direction              "
        "Best Spr %   "
        "Fees %   "
        "Net %   "
        "Depth   "
        "K Spr %   "
        "B Spr %"
    )

    if not rows:
        print("No rows checked.")
        return

    rows = sorted(
        rows,
        key=lambda x: x.get("rough_net_after_fees_pct", -999),
        reverse=True
    )

    for row in rows:
        if row.get("execution_status") != "OK":
            print(f"{row['symbol']:<12} execution_status={row.get('execution_status')}")
            continue

        print(
            f"{row['symbol']:<12}"
            f"{row['best_direction']:<28}"
            f"{row['best_spread_pct']:>10.4f}   "
            f"{ROUND_TRIP_BOTH_EXCHANGES_FEES_PCT:>6.4f}   "
            f"{row['rough_net_after_fees_pct']:>7.4f}   "
            f"{row['combined_depth_score']:>5.0f}   "
            f"{row['kucoin_book_spread_pct']:>7.4f}   "
            f"{row['binance_book_spread_pct']:>7.4f}"
        )


# ------------------------------------------------------------
# Main
# ------------------------------------------------------------

def run_once():
    timestamp = utc_now().isoformat()
    print(f"\n[{timestamp}]")

    kucoin_rows = get_latest_funding_rows(KUCOIN_DB_PATH, "kucoin")
    binance_rows = get_latest_funding_rows(BINANCE_DB_PATH, "binance")

    candidate_rows = build_candidate_rows(
        kucoin_rows=kucoin_rows,
        binance_rows=binance_rows,
    )

    rows_to_check = candidate_rows[:MAX_SYMBOLS_TO_CHECK]

    checked_rows = []

    for row in rows_to_check:
        try:
            checked_rows.append(add_live_spread_check(row))
        except Exception as exc:
            row["execution_status"] = f"ERROR: {exc}"
            checked_rows.append(row)

    print_summary(
        kucoin_rows=kucoin_rows,
        binance_rows=binance_rows,
        candidate_rows=candidate_rows,
        checked_rows=checked_rows,
    )

    print_mark_spread_table(candidate_rows)
    print_live_spread_table(checked_rows)
    print_notional_simulation_table(checked_rows)


def main():
    while True:
        run_once()
        time.sleep(SCAN_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()