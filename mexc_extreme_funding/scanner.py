from __future__ import annotations

import csv
from datetime import timedelta
from pathlib import Path

from mexc_extreme_funding.basis_history import append_basis_observation, calculate_basis_stats
from mexc_extreme_funding.config import DEFAULT_CONFIG, MexcExtremeFundingConfig
from mexc_extreme_funding.mexc_public_client import MexcPublicClient
from mexc_extreme_funding.models import (
    FundingSnapshot, OpportunityRow, benefit_for_direction, iso, parse_datetime, parse_float, utc_now,
)
from mexc_extreme_funding.orderbook import estimate_basis_round_trip
from mexc_extreme_funding.paper_store import PaperStore


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

OPPORTUNITY_FIELDS = [
    "timestamp_utc", "event_key", "base", "direction", "spot_symbol", "perp_symbol",
    "funding_rate_pct", "predicted_funding_rate_pct", "funding_time_utc",
    "funding_interval_hours", "minutes_to_funding", "basis_pct", "notional_usd",
    "spot_entry_avg_price", "perp_entry_avg_price", "spot_exit_avg_price",
    "perp_exit_avg_price", "spot_entry_slippage_pct", "perp_entry_slippage_pct",
    "spot_exit_slippage_pct", "perp_exit_slippage_pct", "expected_edge_pct",
    "round_trip_fillable", "decision", "reason", "basis_observation_count",
    "basis_mean_pct", "basis_std_pct", "basis_percentile", "basis_trend_pct",
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


def latest_snapshot_path(config: MexcExtremeFundingConfig = DEFAULT_CONFIG) -> Path:
    return config.data_dir / "latest_snapshots.csv"


def load_latest_snapshots(config: MexcExtremeFundingConfig = DEFAULT_CONFIG) -> list[FundingSnapshot]:
    return [FundingSnapshot.from_csv_row(row) for row in _read_csv(latest_snapshot_path(config))]


def latest_opportunities_path(config: MexcExtremeFundingConfig = DEFAULT_CONFIG) -> Path:
    return config.data_dir / "latest_opportunities.csv"


def load_latest_opportunities(config: MexcExtremeFundingConfig = DEFAULT_CONFIG) -> list[OpportunityRow]:
    return [OpportunityRow.from_csv_row(row) for row in _read_csv(latest_opportunities_path(config))]


def _entry_decision(
    *, snapshot: FundingSnapshot, direction: str, expected_edge_pct: float,
    exit_cost_pct: float, fillable: bool, observation_count: int,
    percentile: float | None, config: MexcExtremeFundingConfig,
) -> tuple[str, str]:
    rate = snapshot.current_funding_rate_pct
    if not snapshot.eligible or direction != snapshot.direction:
        return "REJECT", "open_position_watchlist"
    if rate is None or benefit_for_direction(direction, rate) < config.min_abs_funding_rate_pct:
        return "REJECT", "funding_below_threshold"
    if expected_edge_pct < config.min_expected_edge_pct:
        return "REJECT", "expected_edge_below_threshold"
    if not fillable:
        return "REJECT", "round_trip_not_fillable"
    if exit_cost_pct > config.max_entry_exit_cost_pct:
        return "REJECT", "exit_cost_too_high"
    if observation_count >= config.min_basis_observations_for_stats:
        if percentile is None:
            return "REJECT", "basis_stats_missing"
        if direction == "SHORT_SPOT_LONG_PERP" and percentile > config.short_spot_entry_max_basis_percentile:
            return "REJECT", "basis_not_low_enough_for_short_spot"
        if direction == "LONG_SPOT_SHORT_PERP" and percentile < config.long_spot_entry_min_basis_percentile:
            return "REJECT", "basis_not_high_enough_for_long_spot"
    return "ENTER_CANDIDATE", "entry_rules_passed"


def _open_position_watchlist(config: MexcExtremeFundingConfig) -> dict[str, dict[str, set[float]]]:
    watchlist: dict[str, dict[str, set[float]]] = {}
    for position in PaperStore(config).load_positions():
        if position.status != "OPEN":
            continue
        notionals = watchlist.setdefault(position.base, {}).setdefault(position.direction, set())
        notionals.add(position.notional_usd)
        notionals.update(config.gentle_unwind_chunk_ladder_usd)
    return watchlist


def _build_opportunities(
    client: MexcPublicClient,
    snapshots: list[FundingSnapshot],
    config: MexcExtremeFundingConfig,
) -> tuple[list[OpportunityRow], list[str]]:
    watchlist = _open_position_watchlist(config)
    rows: list[OpportunityRow] = []
    errors: list[str] = []
    for snapshot in snapshots:
        watched = watchlist.get(snapshot.base, {})
        entry_direction = snapshot.direction if snapshot.eligible else ""
        directions = set(watched)
        if entry_direction:
            directions.add(entry_direction)
        if not directions or not snapshot.spot_symbol:
            continue
        try:
            spot_book, perp_book = client.fetch_orderbooks(
                snapshot.spot_symbol, snapshot.perp_symbol, snapshot.observed_at_utc, limit=100,
            )
            spot_mid = (spot_book.bids[0].price + spot_book.asks[0].price) / 2
            perp_mid = (perp_book.bids[0].price + perp_book.asks[0].price) / 2
            basis_pct = (perp_mid / spot_mid - 1) * 100
            append_basis_observation(
                config=config, base=snapshot.base, spot_symbol=snapshot.spot_symbol,
                perp_symbol=snapshot.perp_symbol, spot_mid=spot_mid, perp_mid=perp_mid,
                basis_pct=basis_pct, funding_rate_pct=snapshot.current_funding_rate_pct,
                minutes_to_funding=snapshot.minutes_to_funding,
            )
            stats = calculate_basis_stats(config=config, base=snapshot.base, current_basis_pct=basis_pct)
            notionals = set(config.layer_ladder_usd)
            for watched_notionals in watched.values():
                notionals.update(watched_notionals)
            for direction in sorted(directions):
                for notional in sorted(value for value in notionals if value > 0):
                    estimate = estimate_basis_round_trip(
                        direction=direction, spot_book=spot_book, perp_book=perp_book,
                        notional_usd=notional,
                    )
                    rate = snapshot.current_funding_rate_pct or 0.0
                    expected_edge = (
                        benefit_for_direction(direction, rate)
                        - estimate.spot_entry.slippage_pct - estimate.perp_entry.slippage_pct
                        - estimate.spot_exit.slippage_pct - estimate.perp_exit.slippage_pct
                        - config.round_trip_fees_pct
                    )
                    exit_cost = (
                        estimate.spot_exit.slippage_pct + estimate.perp_exit.slippage_pct
                        + config.estimated_exit_fee_pct
                    )
                    decision, reason = _entry_decision(
                        snapshot=snapshot, direction=direction, expected_edge_pct=expected_edge,
                        exit_cost_pct=exit_cost, fillable=estimate.round_trip_fillable,
                        observation_count=stats.observation_count, percentile=stats.percentile,
                        config=config,
                    )
                    if notional not in config.layer_ladder_usd and decision == "ENTER_CANDIDATE":
                        decision, reason = "REJECT", "open_position_watchlist"
                    event_key = f"{snapshot.perp_symbol}|{iso(snapshot.next_funding_time_utc)}|{direction}"
                    rows.append(OpportunityRow(
                        timestamp_utc=snapshot.observed_at_utc, event_key=event_key,
                        base=snapshot.base, direction=direction, spot_symbol=snapshot.spot_symbol,
                        perp_symbol=snapshot.perp_symbol, funding_rate_pct=snapshot.current_funding_rate_pct,
                        predicted_funding_rate_pct=snapshot.predicted_funding_rate_pct,
                        funding_time_utc=snapshot.next_funding_time_utc,
                        funding_interval_hours=snapshot.funding_interval_hours,
                        minutes_to_funding=snapshot.minutes_to_funding, basis_pct=basis_pct,
                        notional_usd=notional,
                        spot_entry_avg_price=estimate.spot_entry.average_price,
                        perp_entry_avg_price=estimate.perp_entry.average_price,
                        spot_exit_avg_price=estimate.spot_exit.average_price,
                        perp_exit_avg_price=estimate.perp_exit.average_price,
                        spot_entry_slippage_pct=estimate.spot_entry.slippage_pct,
                        perp_entry_slippage_pct=estimate.perp_entry.slippage_pct,
                        spot_exit_slippage_pct=estimate.spot_exit.slippage_pct,
                        perp_exit_slippage_pct=estimate.perp_exit.slippage_pct,
                        expected_edge_pct=expected_edge,
                        round_trip_fillable=estimate.round_trip_fillable,
                        decision=decision, reason=reason,
                        basis_observation_count=stats.observation_count,
                        basis_mean_pct=stats.mean_pct, basis_std_pct=stats.std_pct,
                        basis_percentile=stats.percentile, basis_trend_pct=stats.trend_pct,
                    ))
        except Exception as error:
            errors.append(f"{snapshot.perp_symbol}: {error}")
    return rows, errors


def _reconcile_settlements(client: MexcPublicClient, config: MexcExtremeFundingConfig) -> int:
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
    config: MexcExtremeFundingConfig = DEFAULT_CONFIG,
    client: MexcPublicClient | None = None,
) -> dict:
    client = client or MexcPublicClient(config)
    now = utc_now()
    snapshots = client.fetch_snapshots(now)
    rows = [snapshot.to_csv_row() for snapshot in snapshots]
    daily_path = config.snapshots_dir / f"snapshots_{now:%Y%m%d}.csv"
    _write_csv(daily_path, rows, SNAPSHOT_FIELDS, append=True)
    _write_csv(latest_snapshot_path(config), rows, SNAPSHOT_FIELDS, append=False)
    opportunities, errors = _build_opportunities(client, snapshots, config)
    opportunity_rows = [row.to_csv_row() for row in opportunities]
    opportunity_path = config.opportunities_dir / f"opportunities_{now:%Y%m%d}.csv"
    _write_csv(opportunity_path, opportunity_rows, OPPORTUNITY_FIELDS, append=True)
    _write_csv(latest_opportunities_path(config), opportunity_rows, OPPORTUNITY_FIELDS, append=False)
    comparisons = _reconcile_settlements(client, config)
    return {
        "snapshots": len(snapshots),
        "eligible": sum(snapshot.eligible for snapshot in snapshots),
        "comparisons": comparisons,
        "opportunities": len(opportunities),
        "errors": errors,
        "path": str(daily_path),
    }
