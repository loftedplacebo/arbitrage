# db.py

import sqlite3
from config import DATABASE_PATH


def get_connection():
    return sqlite3.connect(DATABASE_PATH)


def initialise_database():
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS symbols (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            exchange TEXT NOT NULL,
            symbol TEXT NOT NULL,
            base_currency TEXT,
            quote_currency TEXT,
            status TEXT,
            contract_type TEXT,
            multiplier REAL,
            max_leverage REAL,
            tick_size REAL,
            lot_size REAL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(exchange, symbol)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS funding_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            exchange TEXT NOT NULL,
            symbol TEXT NOT NULL,
            mark_price REAL,
            index_price REAL,
            funding_rate REAL,
            next_funding_time INTEGER,
            time_to_funding_minutes REAL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS orderbook_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            exchange TEXT NOT NULL,
            symbol TEXT NOT NULL,
            best_bid REAL,
            best_ask REAL,
            mid_price REAL,
            spread_pct REAL,
            bid_depth_1pct REAL,
            ask_depth_1pct REAL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS opportunity_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            exchange TEXT NOT NULL,
            symbol TEXT NOT NULL,
            funding_rate_pct REAL,
            time_to_funding_minutes REAL,
            spread_pct REAL,
            estimated_fees_pct REAL,
            rough_net_edge_pct REAL,
            liquidity_score REAL,
            opportunity_score REAL
        )
    """)

    conn.commit()
    conn.close()


def upsert_symbol(symbol_data):
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO symbols (
            exchange,
            symbol,
            base_currency,
            quote_currency,
            status,
            contract_type,
            multiplier,
            max_leverage,
            tick_size,
            lot_size
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(exchange, symbol) DO UPDATE SET
            base_currency = excluded.base_currency,
            quote_currency = excluded.quote_currency,
            status = excluded.status,
            contract_type = excluded.contract_type,
            multiplier = excluded.multiplier,
            max_leverage = excluded.max_leverage,
            tick_size = excluded.tick_size,
            lot_size = excluded.lot_size
    """, (
        symbol_data.get("exchange"),
        symbol_data.get("symbol"),
        symbol_data.get("base_currency"),
        symbol_data.get("quote_currency"),
        symbol_data.get("status"),
        symbol_data.get("contract_type"),
        symbol_data.get("multiplier"),
        symbol_data.get("max_leverage"),
        symbol_data.get("tick_size"),
        symbol_data.get("lot_size"),
    ))

    conn.commit()
    conn.close()


def insert_funding_snapshot(row):
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO funding_snapshots (
            timestamp,
            exchange,
            symbol,
            mark_price,
            index_price,
            funding_rate,
            next_funding_time,
            time_to_funding_minutes
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        row["timestamp"],
        row["exchange"],
        row["symbol"],
        row["mark_price"],
        row["index_price"],
        row["funding_rate"],
        row["next_funding_time"],
        row["time_to_funding_minutes"],
    ))

    conn.commit()
    conn.close()


def insert_orderbook_snapshot(row):
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO orderbook_snapshots (
            timestamp,
            exchange,
            symbol,
            best_bid,
            best_ask,
            mid_price,
            spread_pct,
            bid_depth_1pct,
            ask_depth_1pct
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        row["timestamp"],
        row["exchange"],
        row["symbol"],
        row["best_bid"],
        row["best_ask"],
        row["mid_price"],
        row["spread_pct"],
        row["bid_depth_1pct"],
        row["ask_depth_1pct"],
    ))

    conn.commit()
    conn.close()


def insert_opportunity_snapshot(row):
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO opportunity_snapshots (
            timestamp,
            exchange,
            symbol,
            funding_rate_pct,
            time_to_funding_minutes,
            spread_pct,
            estimated_fees_pct,
            rough_net_edge_pct,
            liquidity_score,
            opportunity_score
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        row["timestamp"],
        row["exchange"],
        row["symbol"],
        row["funding_rate_pct"],
        row["time_to_funding_minutes"],
        row["spread_pct"],
        row["estimated_fees_pct"],
        row["rough_net_edge_pct"],
        row["liquidity_score"],
        row["opportunity_score"],
    ))

    conn.commit()
    conn.close()