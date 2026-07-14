from __future__ import annotations

import csv
from datetime import timedelta
from pathlib import Path

from binance_extreme_funding.binance_public_client import BinancePublicClient
from binance_extreme_funding.config import BinanceExtremeFundingConfig, DEFAULT_CONFIG
from binance_extreme_funding.models import FundingSnapshot, parse_datetime, parse_float, utc_now


SNAPSHOT_FIELDS = [
    "observed_at_utc", "exchange", "base", "spot_symbol", "perp_symbol", "direction",
    "current_funding_rate_pct", "predicted_funding_rate_pct", "next_funding_time_utc",
    "minutes_to_funding", "funding_interval_hours", "index_price", "mark_price",
    "mark_index_basis_pct", "spot_bid", "spot_ask", "perp_bid", "perp_ask",
    "executable_basis_pct", "eligible", "reason", "event_key",
]

COMPARISON_FIELDS = [
    "comparison_key", "exchange", "event_key", "perp_symbol", "direction",
    "observed_at_utc", "funding_time_utc", "minutes_before_funding",
    "displayed_rate_pct", "actual_rate_pct", "absolute_error_pct", "same_direction",
    "mark_index_basis_pct", "executable_basis_pct",
]


def _write_csv(path: Path, rows: list[dict], fields: list[str], append: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = append and path.exists() and path.stat().st_size > 0
    with path.open("a" if append else "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        if not exists:
            writer.writeheader()
        writer.writerows({field: row.get(field, "") for field in fields} for row in rows)


def _read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def latest_snapshot_path(config: BinanceExtremeFundingConfig = DEFAULT_CONFIG) -> Path:
    return config.data_dir / "latest_snapshots.csv"


def load_latest_snapshots(config: BinanceExtremeFundingConfig = DEFAULT_CONFIG) -> list[FundingSnapshot]:
    return [FundingSnapshot.from_csv_row(row) for row in _read_csv(latest_snapshot_path(config))]


def _reconcile_settlements(client: BinancePublicClient, config: BinanceExtremeFundingConfig) -> int:
    now = utc_now()
    comparison_path = config.data_dir / "settlement_comparisons.csv"
    completed = {row.get("comparison_key", "") for row in _read_csv(comparison_path)}
    candidates: dict[str, list[dict]] = {}
    for path in sorted(config.snapshots_dir.glob("snapshots_*.csv"))[-2:]:
        for row in _read_csv(path):
            funding_time = parse_datetime(row.get("next_funding_time_utc"))
            observed = parse_datetime(row.get("observed_at_utc"))
            rate = parse_float(row.get("predicted_funding_rate_pct"))
            if funding_time is None or observed is None or rate is None:
                continue
            if funding_time > now or funding_time < now - timedelta(hours=24):
                continue
            if abs(rate) < config.min_abs_funding_rate_pct:
                continue
            key = f"{row.get('observed_at_utc')}|{row.get('event_key')}"
            if key not in completed:
                candidates.setdefault(row.get("event_key", ""), []).append(row)

    output: list[dict] = []
    for event_rows in candidates.values():
        first = event_rows[0]
        funding_time = parse_datetime(first.get("next_funding_time_utc"))
        if funding_time is None:
            continue
        actual = client.fetch_settled_rate(first.get("perp_symbol", ""), funding_time)
        if actual is None:
            continue
        for row in event_rows:
            displayed = parse_float(row.get("predicted_funding_rate_pct")) or 0.0
            observed = parse_datetime(row.get("observed_at_utc"))
            comparison_key = f"{row.get('observed_at_utc')}|{row.get('event_key')}"
            output.append({
                "comparison_key": comparison_key,
                "exchange": config.exchange,
                "event_key": row.get("event_key", ""),
                "perp_symbol": row.get("perp_symbol", ""),
                "direction": row.get("direction", ""),
                "observed_at_utc": row.get("observed_at_utc", ""),
                "funding_time_utc": row.get("next_funding_time_utc", ""),
                "minutes_before_funding": "" if observed is None else (funding_time - observed).total_seconds() / 60,
                "displayed_rate_pct": displayed,
                "actual_rate_pct": actual,
                "absolute_error_pct": abs(actual - displayed),
                "same_direction": str(actual * displayed > 0),
                "mark_index_basis_pct": row.get("mark_index_basis_pct", ""),
                "executable_basis_pct": row.get("executable_basis_pct", ""),
            })
    if output:
        _write_csv(comparison_path, output, COMPARISON_FIELDS, append=True)
    return len(output)


def scan_once(
    config: BinanceExtremeFundingConfig = DEFAULT_CONFIG,
    client: BinancePublicClient | None = None,
) -> dict:
    client = client or BinancePublicClient(config)
    now = utc_now()
    snapshots = client.fetch_snapshots(now)
    rows = [snapshot.to_csv_row() for snapshot in snapshots]
    daily_path = config.snapshots_dir / f"snapshots_{now:%Y%m%d}.csv"
    _write_csv(daily_path, rows, SNAPSHOT_FIELDS, append=True)
    _write_csv(latest_snapshot_path(config), rows, SNAPSHOT_FIELDS, append=False)
    comparisons = _reconcile_settlements(client, config)
    return {
        "snapshots": len(snapshots),
        "eligible": sum(snapshot.eligible for snapshot in snapshots),
        "comparisons": comparisons,
        "path": str(daily_path),
    }
