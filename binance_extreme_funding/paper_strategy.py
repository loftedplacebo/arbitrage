from __future__ import annotations

import math
import uuid
from datetime import datetime

from binance_extreme_funding.binance_public_client import BinancePublicClient
from binance_extreme_funding.config import BinanceExtremeFundingConfig, DEFAULT_CONFIG
from binance_extreme_funding.models import (
    FundingSnapshot,
    PaperPosition,
    benefit_for_direction,
    iso,
    parse_datetime,
    parse_float,
    parse_int,
    utc_now,
)
from binance_extreme_funding.paper_store import PaperStore
from binance_extreme_funding.scanner import load_latest_snapshots


def _update_signals(
    store: PaperStore,
    snapshots: list[FundingSnapshot],
    now: datetime,
) -> dict[str, dict]:
    signals = store.load_signals()
    latest_by_symbol = {snapshot.perp_symbol: snapshot for snapshot in snapshots}
    for signal in signals.values():
        funding_time = parse_datetime(signal.get("funding_time_utc"))
        current = latest_by_symbol.get(signal.get("perp_symbol", ""))
        if funding_time is not None and funding_time <= now:
            signal["status"] = "SETTLED"
        elif current is None:
            signal["status"] = "STALE"
        elif current.direction != signal.get("direction"):
            signal["status"] = "REVERSED"
        elif not current.eligible:
            signal["status"] = "WATCH"

    for snapshot in snapshots:
        if not snapshot.eligible:
            continue
        key = snapshot.event_key
        rate = snapshot.current_funding_rate_pct or 0.0
        signal = signals.get(key)
        if signal is None:
            signals[key] = {
                "event_key": key,
                "base": snapshot.base,
                "perp_symbol": snapshot.perp_symbol,
                "direction": snapshot.direction,
                "funding_time_utc": iso(snapshot.next_funding_time_utc),
                "first_seen_utc": iso(snapshot.observed_at_utc),
                "last_seen_utc": iso(snapshot.observed_at_utc),
                "observations": 1,
                "first_rate_pct": rate,
                "latest_rate_pct": rate,
                "min_abs_rate_pct": abs(rate),
                "max_abs_rate_pct": abs(rate),
                "status": "ACTIVE",
            }
            continue
        last_seen = parse_datetime(signal.get("last_seen_utc"))
        if last_seen is None or snapshot.observed_at_utc > last_seen:
            signal["observations"] = parse_int(signal.get("observations")) + 1
            signal["last_seen_utc"] = iso(snapshot.observed_at_utc)
        signal["latest_rate_pct"] = rate
        signal["min_abs_rate_pct"] = min(parse_float(signal.get("min_abs_rate_pct"), abs(rate)) or abs(rate), abs(rate))
        signal["max_abs_rate_pct"] = max(parse_float(signal.get("max_abs_rate_pct"), abs(rate)) or abs(rate), abs(rate))
        signal["status"] = "ACTIVE"
    store.write_signals(signals)
    return signals


def _basis_pnl(direction: str, entry: float, current: float) -> float:
    if direction == "LONG_SPOT_SHORT_PERP":
        return entry - current
    return current - entry


def _close_basis(snapshot: FundingSnapshot, direction: str) -> float | None:
    if direction == "LONG_SPOT_SHORT_PERP":
        reference, derivative = snapshot.spot_bid, snapshot.perp_ask
    else:
        reference, derivative = snapshot.spot_ask, snapshot.perp_bid
    if reference is None or derivative is None or reference <= 0:
        return None
    return (derivative / reference - 1) * 100


def _mark_and_exit_positions(
    *,
    positions: list[PaperPosition],
    snapshots: list[FundingSnapshot],
    store: PaperStore,
    client: BinancePublicClient,
    config: BinanceExtremeFundingConfig,
    now: datetime,
) -> tuple[int, int]:
    latest = {snapshot.perp_symbol: snapshot for snapshot in snapshots}
    history_cache: dict[tuple[str, str], float | None] = {}
    funding_captures = 0
    exits = 0
    for position in positions:
        if position.status != "OPEN":
            continue
        snapshot = latest.get(position.perp_symbol)
        close_basis = None if snapshot is None else _close_basis(snapshot, position.direction)
        if close_basis is not None:
            position.current_basis_pct = close_basis
        position.basis_pnl_pct = _basis_pnl(
            position.direction,
            position.entry_basis_pct,
            position.current_basis_pct,
        )
        position.updated_at_utc = now

        if (
            position.actual_funding_rate_pct is None
            and position.funding_time_utc is not None
            and position.funding_time_utc <= now
        ):
            cache_key = (position.perp_symbol, iso(position.funding_time_utc))
            if cache_key not in history_cache:
                history_cache[cache_key] = client.fetch_settled_rate(position.perp_symbol, position.funding_time_utc)
            actual = history_cache[cache_key]
            if actual is not None:
                position.actual_funding_rate_pct = actual
                position.funding_pnl_pct = benefit_for_direction(position.direction, actual)
                funding_captures += 1
                store.append_funding({
                    "timestamp_utc": iso(now),
                    "position_id": position.position_id,
                    "event_key": position.event_key,
                    "perp_symbol": position.perp_symbol,
                    "funding_time_utc": iso(position.funding_time_utc),
                    "displayed_rate_pct": position.displayed_rate_at_entry_pct,
                    "actual_rate_pct": actual,
                    "funding_benefit_pct": position.funding_pnl_pct,
                    "funding_pnl_usd": position.notional_usd * position.funding_pnl_pct / 100,
                })

        expected_funding = position.funding_pnl_pct
        if position.actual_funding_rate_pct is None and position.funding_time_utc and position.funding_time_utc > now:
            expected_funding = benefit_for_direction(position.direction, position.displayed_rate_at_entry_pct)
        position.estimated_net_pnl_pct = position.basis_pnl_pct + expected_funding - config.round_trip_fees_pct
        realizable_net_pct = position.basis_pnl_pct + position.funding_pnl_pct - config.round_trip_fees_pct
        age_hours = (now - position.entry_at_utc).total_seconds() / 3600
        current_rate = snapshot.current_funding_rate_pct if snapshot is not None else None
        reversed_signal = current_rate is not None and benefit_for_direction(position.direction, current_rate) <= 0

        exit_reason = ""
        if position.basis_pnl_pct >= config.basis_take_profit_pct and realizable_net_pct > 0:
            exit_reason = "basis_take_profit"
        elif reversed_signal and realizable_net_pct > 0:
            exit_reason = "profitable_funding_reversal"
        elif position.actual_funding_rate_pct is not None and realizable_net_pct > 0:
            exit_reason = "funding_captured_profitable"
        elif position.basis_pnl_pct <= -config.max_adverse_basis_pct:
            exit_reason = "max_adverse_basis"
        elif age_hours >= config.max_hold_hours:
            exit_reason = "max_hold"

        if exit_reason:
            position.status = "CLOSED"
            position.exit_at_utc = now
            position.exit_reason = exit_reason
            position.estimated_net_pnl_pct = realizable_net_pct
            position.realised_pnl_usd = position.notional_usd * realizable_net_pct / 100
            exits += 1
            store.append_fill({
                "timestamp_utc": iso(now), "event_type": "EXIT", "position_id": position.position_id,
                "event_key": position.event_key, "perp_symbol": position.perp_symbol,
                "direction": position.direction, "layer_index": position.layer_index,
                "notional_usd": position.notional_usd, "basis_pct": position.current_basis_pct,
                "funding_rate_pct": position.actual_funding_rate_pct,
                "net_pnl_pct": realizable_net_pct, "realised_pnl_usd": position.realised_pnl_usd,
                "reason": exit_reason,
            })
    return funding_captures, exits


def _open_due_layers(
    *,
    positions: list[PaperPosition],
    snapshots: list[FundingSnapshot],
    signals: dict[str, dict],
    store: PaperStore,
    config: BinanceExtremeFundingConfig,
    now: datetime,
) -> int:
    opened = 0
    open_positions = [position for position in positions if position.status == "OPEN"]
    total_open = sum(position.notional_usd for position in open_positions)
    symbol_open: dict[str, float] = {}
    for position in open_positions:
        symbol_open[position.perp_symbol] = symbol_open.get(position.perp_symbol, 0.0) + position.notional_usd

    for snapshot in sorted(
        (item for item in snapshots if item.eligible and item.current_funding_rate_pct is not None),
        key=lambda item: abs(item.current_funding_rate_pct or 0.0),
        reverse=True,
    ):
        signal = signals.get(snapshot.event_key)
        if not signal or signal.get("status") != "ACTIVE":
            continue
        observations = parse_int(signal.get("observations"))
        if observations < config.min_consistent_observations:
            continue
        first_seen = parse_datetime(signal.get("first_seen_utc")) or now
        age_minutes = max(0.0, (now - first_seen).total_seconds() / 60)
        if age_minutes < config.min_signal_age_minutes:
            continue
        due_layers = min(len(config.layer_ladder_usd), 1 + math.floor(age_minutes / config.layer_interval_minutes))
        existing_layers = sum(1 for position in positions if position.event_key == snapshot.event_key)
        if existing_layers >= due_layers or existing_layers >= len(config.layer_ladder_usd):
            continue
        layer_index = existing_layers
        notional = config.layer_ladder_usd[layer_index]
        reason = "layer_due"
        allowed = True
        if snapshot.executable_basis_pct is None:
            allowed, reason = False, "basis_missing"
        elif len(open_positions) >= config.max_open_positions:
            allowed, reason = False, "max_open_positions"
        elif total_open + notional > config.max_total_notional_usd:
            allowed, reason = False, "max_total_notional"
        elif symbol_open.get(snapshot.perp_symbol, 0.0) + notional > config.max_symbol_notional_usd:
            allowed, reason = False, "max_symbol_notional"
        store.append_decision({
            "timestamp_utc": iso(now), "decision": "ENTRY", "event_key": snapshot.event_key,
            "perp_symbol": snapshot.perp_symbol, "allowed": str(allowed), "reason": reason,
            "layer_index": layer_index, "notional_usd": notional,
        })
        if not allowed:
            continue
        position = PaperPosition(
            position_id=f"BN-{uuid.uuid4().hex[:12]}", event_key=snapshot.event_key,
            base=snapshot.base, spot_symbol=snapshot.spot_symbol, perp_symbol=snapshot.perp_symbol,
            direction=snapshot.direction, layer_index=layer_index, notional_usd=notional,
            entry_at_utc=now, updated_at_utc=now, funding_time_utc=snapshot.next_funding_time_utc,
            displayed_rate_at_entry_pct=snapshot.current_funding_rate_pct or 0.0,
            actual_funding_rate_pct=None, entry_basis_pct=snapshot.executable_basis_pct or 0.0,
            current_basis_pct=snapshot.executable_basis_pct or 0.0, basis_pnl_pct=0.0,
            funding_pnl_pct=0.0,
            estimated_net_pnl_pct=abs(snapshot.current_funding_rate_pct or 0.0) - config.round_trip_fees_pct,
            realised_pnl_usd=0.0, status="OPEN", exit_at_utc=None, exit_reason="",
        )
        positions.append(position)
        open_positions.append(position)
        total_open += notional
        symbol_open[snapshot.perp_symbol] = symbol_open.get(snapshot.perp_symbol, 0.0) + notional
        opened += 1
        store.append_fill({
            "timestamp_utc": iso(now), "event_type": "ENTRY", "position_id": position.position_id,
            "event_key": position.event_key, "perp_symbol": position.perp_symbol,
            "direction": position.direction, "layer_index": position.layer_index,
            "notional_usd": position.notional_usd, "basis_pct": position.entry_basis_pct,
            "funding_rate_pct": position.displayed_rate_at_entry_pct,
            "net_pnl_pct": position.estimated_net_pnl_pct, "realised_pnl_usd": 0.0,
            "reason": "consistent_extreme_funding_layer",
        })
    return opened


def run_paper_strategy_once(
    config: BinanceExtremeFundingConfig = DEFAULT_CONFIG,
    client: BinancePublicClient | None = None,
    snapshots: list[FundingSnapshot] | None = None,
    now: datetime | None = None,
) -> dict:
    now = now or utc_now()
    client = client or BinancePublicClient(config)
    snapshots = load_latest_snapshots(config) if snapshots is None else snapshots
    store = PaperStore(config)
    signals = _update_signals(store, snapshots, now)
    positions = store.load_positions()
    funding_captures, exits = _mark_and_exit_positions(
        positions=positions, snapshots=snapshots, store=store, client=client, config=config, now=now,
    )
    opened = _open_due_layers(
        positions=positions, snapshots=snapshots, signals=signals, store=store, config=config, now=now,
    )
    store.write_positions(positions)
    return {
        "snapshots": len(snapshots), "active_signals": sum(row.get("status") == "ACTIVE" for row in signals.values()),
        "opened": opened, "exits": exits, "funding_captures": funding_captures,
        "open_positions": sum(position.status == "OPEN" for position in positions),
    }
