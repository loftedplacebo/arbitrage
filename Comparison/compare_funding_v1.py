# compare_funding.py

import sqlite3
import time
from pathlib import Path
from datetime import datetime, timezone

import requests


# ------------------------------------------------------------
# Paths
# ------------------------------------------------------------

ARBITRAGE_DIR = Path(__file__).resolve().parents[1]

KUCOIN_DB_PATH = ARBITRAGE_DIR / "Kucoin" / "data" / "kucoin_scanner.db"
BINANCE_DB_PATH = ARBITRAGE_DIR / "Binance" / "data" / "binance_scanner.db"

SCAN_INTERVAL_SECONDS = 60

# Funding comparison filters
MIN_ABS_GAP_PCT = 0.03
MAX_DATA_AGE_MINUTES = 5

# Execution check settings
TOP_CANDIDATES_TO_CHECK = 10
MAX_FUNDING_TIME_DIFF_HOURS = 0.50
MAX_ABS_MARK_SPREAD_PCT = 1.00

# Rough fee assumption for opening and closing both legs:
# KuCoin taker open+close = 0.06% * 2 = 0.12%
# Binance taker open+close = 0.05% * 2 = 0.10%
# Total = 0.22%
ROUND_TRIP_BOTH_EXCHANGES_FEES_PCT = 0.22

KUCOIN_FUTURES_BASE_URL = "https://api-futures.kucoin.com"
BINANCE_FUTURES_BASE_URL = "https://fapi.binance.com"

COMPARISON_DATA_DIR = ARBITRAGE_DIR / "Comparison" / "data"
COMPARISON_DATA_DIR.mkdir(exist_ok=True)

COMPARISON_DB_PATH = COMPARISON_DATA_DIR / "comparison_scanner.db"

# ------------------------------------------------------------
# General helpers
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


def normalise_symbol(exchange, symbol):
    """
    KuCoin futures symbols usually end with M, e.g. TONUSDTM.
    Binance uses TONUSDT.
    Common key becomes TONUSDT.
    """
    if not symbol:
        return None

    symbol = symbol.upper()

    if exchange == "kucoin" and symbol.endswith("USDTM"):
        return symbol[:-1]

    return symbol


def safe_float(value, default=None):
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def pct(value):
    if value is None:
        return None
    return float(value) * 100


# ------------------------------------------------------------
# Database reads
# ------------------------------------------------------------

def get_latest_funding_rows(db_path, exchange):
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

        funding_rate = safe_float(row.get("funding_rate"))
        funding_rate_pct = pct(funding_rate)

        output[common_symbol] = {
            "exchange": exchange,
            "raw_symbol": raw_symbol,
            "common_symbol": common_symbol,
            "timestamp": row.get("timestamp"),
            "age_minutes": minutes_since(row.get("timestamp")),
            "mark_price": safe_float(row.get("mark_price")),
            "index_price": safe_float(row.get("index_price")),
            "funding_rate": funding_rate,
            "funding_rate_pct": funding_rate_pct,
            "next_funding_time": row.get("next_funding_time"),
            "time_to_funding_minutes": safe_float(row.get("time_to_funding_minutes")),
        }

    return output


# ------------------------------------------------------------
# Funding comparison logic
# ------------------------------------------------------------

def build_comparison_rows(kucoin_rows, binance_rows):
    common_symbols = sorted(set(kucoin_rows.keys()) & set(binance_rows.keys()))
    rows = []

    for symbol in common_symbols:
        k = kucoin_rows[symbol]
        b = binance_rows[symbol]

        kucoin_funding = k["funding_rate_pct"]
        binance_funding = b["funding_rate_pct"]

        if kucoin_funding is None or binance_funding is None:
            continue

        funding_gap_pct = kucoin_funding - binance_funding
        abs_gap_pct = abs(funding_gap_pct)

        # Funding cashflow:
        # long cashflow = -funding_rate
        # short cashflow = +funding_rate
        #
        # If gap positive, KuCoin funding > Binance:
        # long Binance / short KuCoin receives gap.
        #
        # If gap negative:
        # long KuCoin / short Binance receives abs(gap).
        if funding_gap_pct > 0:
            preferred_long = "Binance"
            preferred_short = "KuCoin"
            summary = "Long Binance / Short KuCoin"
        else:
            preferred_long = "KuCoin"
            preferred_short = "Binance"
            summary = "Long KuCoin / Short Binance"

        kucoin_mark = k["mark_price"]
        binance_mark = b["mark_price"]

        mark_spread_pct = None
        if kucoin_mark and binance_mark:
            mark_spread_pct = ((kucoin_mark - binance_mark) / binance_mark) * 100

        kucoin_age = k["age_minutes"]
        binance_age = b["age_minutes"]

        max_age = max(
            kucoin_age if kucoin_age is not None else 999,
            binance_age if binance_age is not None else 999,
        )

        stale_flag = "STALE" if max_age > MAX_DATA_AGE_MINUTES else "OK"

        kucoin_hours = (
            k["time_to_funding_minutes"] / 60
            if k["time_to_funding_minutes"] is not None
            else None
        )

        binance_hours = (
            b["time_to_funding_minutes"] / 60
            if b["time_to_funding_minutes"] is not None
            else None
        )

        funding_time_diff_hours = None
        if kucoin_hours is not None and binance_hours is not None:
            funding_time_diff_hours = abs(kucoin_hours - binance_hours)

        rows.append({
            "symbol": symbol,
            "kucoin_symbol": k["raw_symbol"],
            "binance_symbol": b["raw_symbol"],
            "kucoin_funding_pct": kucoin_funding,
            "binance_funding_pct": binance_funding,
            "funding_gap_pct": funding_gap_pct,
            "abs_gap_pct": abs_gap_pct,
            "preferred_long": preferred_long,
            "preferred_short": preferred_short,
            "summary": summary,
            "kucoin_mark": kucoin_mark,
            "binance_mark": binance_mark,
            "mark_spread_pct": mark_spread_pct,
            "kucoin_hours_to_funding": kucoin_hours,
            "binance_hours_to_funding": binance_hours,
            "funding_time_diff_hours": funding_time_diff_hours,
            "kucoin_next_funding_time": k["next_funding_time"],
            "binance_next_funding_time": b["next_funding_time"],
            "kucoin_age_minutes": kucoin_age,
            "binance_age_minutes": binance_age,
            "stale_flag": stale_flag,
        })

    return sorted(rows, key=lambda x: x["abs_gap_pct"], reverse=True)


# ------------------------------------------------------------
# Live order book calls
# ------------------------------------------------------------

def get_kucoin_orderbook(symbol):
    url = f"{KUCOIN_FUTURES_BASE_URL}/api/v1/level2/snapshot"

    response = requests.get(
        url,
        params={"symbol": symbol},
        timeout=20,
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
        timeout=20,
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

    if not best_bid or not best_ask:
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
    """
    Approximate notional depth within 1% of mid.
    Note: for futures, exchange contract sizing can differ, so this is an execution-quality proxy,
    not a final trade-size engine.
    """
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
# Execution validation
# ------------------------------------------------------------

def get_orderbook_for_exchange(exchange, symbol):
    if exchange == "KuCoin":
        return get_kucoin_orderbook(symbol)

    if exchange == "Binance":
        return get_binance_orderbook(symbol)

    raise ValueError(f"Unsupported exchange: {exchange}")


def get_raw_symbol_for_exchange(row, exchange):
    if exchange == "KuCoin":
        return row["kucoin_symbol"]

    if exchange == "Binance":
        return row["binance_symbol"]

    raise ValueError(f"Unsupported exchange: {exchange}")


def add_live_execution_check(row):
    long_exchange = row["preferred_long"]
    short_exchange = row["preferred_short"]

    long_symbol = get_raw_symbol_for_exchange(row, long_exchange)
    short_symbol = get_raw_symbol_for_exchange(row, short_exchange)

    long_book = get_orderbook_for_exchange(long_exchange, long_symbol)
    short_book = get_orderbook_for_exchange(short_exchange, short_symbol)

    long_tob = get_top_of_book(long_book)
    short_tob = get_top_of_book(short_book)

    if long_tob is None or short_tob is None:
        row["execution_status"] = "NO_BOOK"
        return row

    # To open:
    # Long leg buys at ask.
    # Short leg sells at bid.
    long_entry_price = long_tob["best_ask"]
    short_entry_price = short_tob["best_bid"]

    reference_price = (long_entry_price + short_entry_price) / 2

    # Positive entry basis is favourable:
    # You short higher than you buy.
    entry_basis_pct = ((short_entry_price - long_entry_price) / reference_price) * 100

    long_depth = estimate_depth_within_1pct(long_book)
    short_depth = estimate_depth_within_1pct(short_book)

    combined_depth_score = min(
        long_depth["depth_score"],
        short_depth["depth_score"],
    )

    # One-period rough net:
    # funding capture + favourable/unfavourable entry basis - full round-trip fees.
    rough_executable_net_pct = (
        row["abs_gap_pct"]
        + entry_basis_pct
        - ROUND_TRIP_BOTH_EXCHANGES_FEES_PCT
    )

    row.update({
        "execution_status": "OK",
        "long_exchange": long_exchange,
        "short_exchange": short_exchange,
        "long_symbol": long_symbol,
        "short_symbol": short_symbol,
        "long_entry_ask": long_entry_price,
        "short_entry_bid": short_entry_price,
        "entry_basis_pct": entry_basis_pct,
        "long_book_spread_pct": long_tob["spread_pct"],
        "short_book_spread_pct": short_tob["spread_pct"],
        "long_depth_score": long_depth["depth_score"],
        "short_depth_score": short_depth["depth_score"],
        "combined_depth_score": combined_depth_score,
        "rough_executable_net_pct": rough_executable_net_pct,
    })

    return row


def select_candidates_for_execution(rows):
    candidates = []

    for row in rows:
        if row["abs_gap_pct"] < MIN_ABS_GAP_PCT:
            continue

        if row["stale_flag"] != "OK":
            continue

        if row["kucoin_hours_to_funding"] is None or row["binance_hours_to_funding"] is None:
            continue

        if row["kucoin_hours_to_funding"] > 8 or row["binance_hours_to_funding"] > 8:
            continue

        if row["funding_time_diff_hours"] is None:
            continue

        if row["funding_time_diff_hours"] > MAX_FUNDING_TIME_DIFF_HOURS:
            continue

        if row["mark_spread_pct"] is not None and abs(row["mark_spread_pct"]) > MAX_ABS_MARK_SPREAD_PCT:
            continue

        candidates.append(row)

    return candidates[:TOP_CANDIDATES_TO_CHECK]


# ------------------------------------------------------------
# Printing
# ------------------------------------------------------------

def print_summary(kucoin_rows, binance_rows, comparison_rows):
    print("\nCross-exchange funding comparison")
    print("---------------------------------")
    print(f"KuCoin DB:           {KUCOIN_DB_PATH}")
    print(f"Binance DB:          {BINANCE_DB_PATH}")
    print(f"KuCoin symbols:      {len(kucoin_rows)}")
    print(f"Binance symbols:     {len(binance_rows)}")
    print(f"Matched symbols:     {len(comparison_rows)}")
    print(f"Minimum gap shown:   {MIN_ABS_GAP_PCT:.4f}%")


def print_top_gap_table(rows):
    filtered = [
        row for row in rows
        if row["abs_gap_pct"] >= MIN_ABS_GAP_PCT
    ]

    print("\nTop cross-exchange funding gaps")
    print(
        "Symbol       "
        "KuCoin %   "
        "Binance %   "
        "Gap %   "
        "Abs Gap %   "
        "Long       "
        "Short      "
        "K Hrs   "
        "B Hrs   "
        "Mark Spr %   "
        "Age"
    )

    for row in filtered[:20]:
        print(
            f"{row['symbol']:<12}"
            f"{row['kucoin_funding_pct']:>9.4f}   "
            f"{row['binance_funding_pct']:>10.4f}   "
            f"{row['funding_gap_pct']:>7.4f}   "
            f"{row['abs_gap_pct']:>9.4f}   "
            f"{row['preferred_long']:<10}"
            f"{row['preferred_short']:<11}"
            f"{(row['kucoin_hours_to_funding'] or 0):>6.2f}   "
            f"{(row['binance_hours_to_funding'] or 0):>6.2f}   "
            f"{(row['mark_spread_pct'] or 0):>10.4f}   "
            f"{row['stale_flag']}"
        )


def print_execution_table(rows):
    print("\nLive execution check for top candidates")
    print(
        "Symbol       "
        "Funding Gap %   "
        "Long       "
        "Short      "
        "Entry Basis %   "
        "Fees %   "
        "Rough Exec Net %   "
        "Depth   "
        "Long Spr %   "
        "Short Spr %"
    )

    if not rows:
        print("No clean candidates passed the execution filters.")
        return

    rows = sorted(
        rows,
        key=lambda x: x.get("rough_executable_net_pct", -999),
        reverse=True
    )

    for row in rows:
        if row.get("execution_status") != "OK":
            print(f"{row['symbol']:<12} execution_status={row.get('execution_status')}")
            continue

        print(
            f"{row['symbol']:<12}"
            f"{row['abs_gap_pct']:>13.4f}   "
            f"{row['long_exchange']:<10}"
            f"{row['short_exchange']:<11}"
            f"{row['entry_basis_pct']:>13.4f}   "
            f"{ROUND_TRIP_BOTH_EXCHANGES_FEES_PCT:>6.4f}   "
            f"{row['rough_executable_net_pct']:>17.4f}   "
            f"{row['combined_depth_score']:>5.0f}   "
            f"{row['long_book_spread_pct']:>10.4f}   "
            f"{row['short_book_spread_pct']:>11.4f}"
        )

def get_comparison_connection():
    return sqlite3.connect(COMPARISON_DB_PATH)


def initialise_comparison_database():
    conn = get_comparison_connection()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS funding_comparison_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            symbol TEXT NOT NULL,
            kucoin_symbol TEXT,
            binance_symbol TEXT,
            kucoin_funding_pct REAL,
            binance_funding_pct REAL,
            funding_gap_pct REAL,
            abs_gap_pct REAL,
            preferred_long TEXT,
            preferred_short TEXT,
            kucoin_hours_to_funding REAL,
            binance_hours_to_funding REAL,
            funding_time_diff_hours REAL,
            kucoin_mark REAL,
            binance_mark REAL,
            mark_spread_pct REAL,
            kucoin_age_minutes REAL,
            binance_age_minutes REAL,
            stale_flag TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS execution_check_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            symbol TEXT NOT NULL,
            funding_gap_pct REAL,
            abs_gap_pct REAL,
            long_exchange TEXT,
            short_exchange TEXT,
            long_symbol TEXT,
            short_symbol TEXT,
            long_entry_ask REAL,
            short_entry_bid REAL,
            entry_basis_pct REAL,
            estimated_fees_pct REAL,
            rough_executable_net_pct REAL,
            long_book_spread_pct REAL,
            short_book_spread_pct REAL,
            long_depth_score REAL,
            short_depth_score REAL,
            combined_depth_score REAL,
            execution_status TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.commit()
    conn.close()


def insert_funding_comparison_snapshot(timestamp, row):
    conn = get_comparison_connection()
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO funding_comparison_snapshots (
            timestamp,
            symbol,
            kucoin_symbol,
            binance_symbol,
            kucoin_funding_pct,
            binance_funding_pct,
            funding_gap_pct,
            abs_gap_pct,
            preferred_long,
            preferred_short,
            kucoin_hours_to_funding,
            binance_hours_to_funding,
            funding_time_diff_hours,
            kucoin_mark,
            binance_mark,
            mark_spread_pct,
            kucoin_age_minutes,
            binance_age_minutes,
            stale_flag
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        timestamp,
        row.get("symbol"),
        row.get("kucoin_symbol"),
        row.get("binance_symbol"),
        row.get("kucoin_funding_pct"),
        row.get("binance_funding_pct"),
        row.get("funding_gap_pct"),
        row.get("abs_gap_pct"),
        row.get("preferred_long"),
        row.get("preferred_short"),
        row.get("kucoin_hours_to_funding"),
        row.get("binance_hours_to_funding"),
        row.get("funding_time_diff_hours"),
        row.get("kucoin_mark"),
        row.get("binance_mark"),
        row.get("mark_spread_pct"),
        row.get("kucoin_age_minutes"),
        row.get("binance_age_minutes"),
        row.get("stale_flag"),
    ))

    conn.commit()
    conn.close()


def insert_execution_check_snapshot(timestamp, row):
    conn = get_comparison_connection()
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO execution_check_snapshots (
            timestamp,
            symbol,
            funding_gap_pct,
            abs_gap_pct,
            long_exchange,
            short_exchange,
            long_symbol,
            short_symbol,
            long_entry_ask,
            short_entry_bid,
            entry_basis_pct,
            estimated_fees_pct,
            rough_executable_net_pct,
            long_book_spread_pct,
            short_book_spread_pct,
            long_depth_score,
            short_depth_score,
            combined_depth_score,
            execution_status
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        timestamp,
        row.get("symbol"),
        row.get("funding_gap_pct"),
        row.get("abs_gap_pct"),
        row.get("long_exchange"),
        row.get("short_exchange"),
        row.get("long_symbol"),
        row.get("short_symbol"),
        row.get("long_entry_ask"),
        row.get("short_entry_bid"),
        row.get("entry_basis_pct"),
        ROUND_TRIP_BOTH_EXCHANGES_FEES_PCT,
        row.get("rough_executable_net_pct"),
        row.get("long_book_spread_pct"),
        row.get("short_book_spread_pct"),
        row.get("long_depth_score"),
        row.get("short_depth_score"),
        row.get("combined_depth_score"),
        row.get("execution_status"),
    ))

    conn.commit()
    conn.close()
# ------------------------------------------------------------
# Main loop
# ------------------------------------------------------------

def run_once():
    kucoin_rows = get_latest_funding_rows(KUCOIN_DB_PATH, "kucoin")
    binance_rows = get_latest_funding_rows(BINANCE_DB_PATH, "binance")

    comparison_rows = build_comparison_rows(
        kucoin_rows=kucoin_rows,
        binance_rows=binance_rows,
    )

    timestamp = utc_now().isoformat()
    print(f"\n[{timestamp}]")

    for row in comparison_rows:
        if row["abs_gap_pct"] >= MIN_ABS_GAP_PCT:
            insert_funding_comparison_snapshot(timestamp, row)

    print_summary(
        kucoin_rows=kucoin_rows,
        binance_rows=binance_rows,
        comparison_rows=comparison_rows,
    )

    print_top_gap_table(comparison_rows)

    execution_candidates = select_candidates_for_execution(comparison_rows)

    checked_rows = []

    for row in execution_candidates:
        try:
            checked_row = add_live_execution_check(row)
            checked_rows.append(checked_row)
            insert_execution_check_snapshot(timestamp, checked_row)

        except Exception as exc:
            row["execution_status"] = f"ERROR: {exc}"
            checked_rows.append(row)
            insert_execution_check_snapshot(timestamp, row)

        print_execution_table(checked_rows)


def main():
    initialise_comparison_database()

    print(f"Comparison DB: {COMPARISON_DB_PATH}")

    while True:
        run_once()
        time.sleep(SCAN_INTERVAL_SECONDS)

if __name__ == "__main__":
    main()