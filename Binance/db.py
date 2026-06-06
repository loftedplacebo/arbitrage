# db.py

import sqlite3

from config import DATABASE_PATH


def get_connection():
    return sqlite3.connect(DATABASE_PATH)


def initialise_database():
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS funding_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            exchange TEXT NOT NULL,
            symbol TEXT NOT NULL,
            mark_price REAL,
            index_price REAL,
            funding_rate REAL,
            funding_rate_pct REAL,
            next_funding_time INTEGER,
            time_to_funding_minutes REAL,
            premium_pct REAL,
            quote_volume_24h REAL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
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
            ask_depth_1pct REAL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS opportunity_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            exchange TEXT NOT NULL,
            symbol TEXT NOT NULL,
            funding_rate_pct REAL,
            absolute_funding_rate_pct REAL,
            time_to_funding_minutes REAL,
            spread_pct REAL,
            estimated_fees_pct REAL,
            rough_net_edge_pct REAL,
            liquidity_score REAL,
            opportunity_score REAL,
            quote_volume_24h REAL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

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
            funding_rate_pct,
            next_funding_time,
            time_to_funding_minutes,
            premium_pct,
            quote_volume_24h
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        row["timestamp"],
        row["exchange"],
        row["symbol"],
        row["mark_price"],
        row["index_price"],
        row["funding_rate"],
        row["funding_rate_pct"],
        row["next_funding_time"],
        row["time_to_funding_minutes"],
        row["premium_pct"],
        row["quote_volume_24h"],
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
            absolute_funding_rate_pct,
            time_to_funding_minutes,
            spread_pct,
            estimated_fees_pct,
            rough_net_edge_pct,
            liquidity_score,
            opportunity_score,
            quote_volume_24h
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        row["timestamp"],
        row["exchange"],
        row["symbol"],
        row["funding_rate_pct"],
        row["absolute_funding_rate_pct"],
        row["time_to_funding_minutes"],
        row["spread_pct"],
        row["estimated_fees_pct"],
        row["rough_net_edge_pct"],
        row["liquidity_score"],
        row["opportunity_score"],
        row["quote_volume_24h"],
    ))

    conn.commit()
    conn.close()