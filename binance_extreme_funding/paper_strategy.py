from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from binance_extreme_funding.binance_public_client import BinancePublicClient
from binance_extreme_funding.config import BinanceExtremeFundingConfig, DEFAULT_CONFIG
from binance_extreme_funding.models import (
    FundingSnapshot,
    OpportunityRow,
    PaperPosition,
    benefit_for_direction,
    iso,
    parse_datetime,
    parse_float,
    parse_int,
    utc_now,
)
from binance_extreme_funding.paper_store import PaperStore
from binance_extreme_funding.scanner import load_latest_opportunities, load_latest_snapshots


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
                "event_key": key, "base": snapshot.base, "perp_symbol": snapshot.perp_symbol,
                "direction": snapshot.direction, "funding_time_utc": iso(snapshot.next_funding_time_utc),
                "first_seen_utc": iso(snapshot.observed_at_utc),
                "last_seen_utc": iso(snapshot.observed_at_utc), "observations": 1,
                "first_rate_pct": rate, "latest_rate_pct": rate,
                "min_abs_rate_pct": abs(rate), "max_abs_rate_pct": abs(rate), "status": "ACTIVE",
            }
            continue
        last_seen = parse_datetime(signal.get("last_seen_utc"))
        if last_seen is None or snapshot.observed_at_utc > last_seen:
            signal["observations"] = parse_int(signal.get("observations")) + 1
            signal["last_seen_utc"] = iso(snapshot.observed_at_utc)
        signal["latest_rate_pct"] = rate
        signal["min_abs_rate_pct"] = min(
            parse_float(signal.get("min_abs_rate_pct"), abs(rate)) or abs(rate), abs(rate),
        )
        signal["max_abs_rate_pct"] = max(
            parse_float(signal.get("max_abs_rate_pct"), abs(rate)) or abs(rate), abs(rate),
        )
        signal["status"] = "ACTIVE"
    store.write_signals(signals)
    return signals


def _synthetic_opportunities(
    snapshots: list[FundingSnapshot],
    config: BinanceExtremeFundingConfig,
) -> list[OpportunityRow]:
    """Build deterministic top-book rows for unit tests; production uses scanner depth rows."""
    rows: list[OpportunityRow] = []
    notionals = set(config.layer_ladder_usd) | set(config.gentle_unwind_chunk_ladder_usd)
    for snapshot in snapshots:
        if snapshot.current_funding_rate_pct is None or not snapshot.direction:
            continue
        direction = snapshot.direction
        if direction == "LONG_SPOT_SHORT_PERP":
            spot_entry, perp_entry = snapshot.spot_ask, snapshot.perp_bid
            spot_exit, perp_exit = snapshot.spot_bid, snapshot.perp_ask
        else:
            spot_entry, perp_entry = snapshot.spot_bid, snapshot.perp_ask
            spot_exit, perp_exit = snapshot.spot_ask, snapshot.perp_bid
        for notional in sorted(notionals):
            fillable = all(
                value is not None and value > 0
                for value in (spot_entry, perp_entry, spot_exit, perp_exit)
            )
            edge = abs(snapshot.current_funding_rate_pct) - config.round_trip_fees_pct
            rows.append(OpportunityRow(
                timestamp_utc=snapshot.observed_at_utc, event_key=snapshot.event_key,
                base=snapshot.base, direction=direction, spot_symbol=snapshot.spot_symbol,
                perp_symbol=snapshot.perp_symbol, funding_rate_pct=snapshot.current_funding_rate_pct,
                predicted_funding_rate_pct=snapshot.predicted_funding_rate_pct,
                funding_time_utc=snapshot.next_funding_time_utc,
                funding_interval_hours=snapshot.funding_interval_hours,
                minutes_to_funding=snapshot.minutes_to_funding,
                basis_pct=snapshot.executable_basis_pct, notional_usd=notional,
                spot_entry_avg_price=spot_entry, perp_entry_avg_price=perp_entry,
                spot_exit_avg_price=spot_exit, perp_exit_avg_price=perp_exit,
                spot_entry_slippage_pct=0.0, perp_entry_slippage_pct=0.0,
                spot_exit_slippage_pct=0.0, perp_exit_slippage_pct=0.0,
                expected_edge_pct=edge, round_trip_fillable=fillable,
                decision="ENTER_CANDIDATE" if snapshot.eligible else "REJECT",
                reason="entry_rules_passed" if snapshot.eligible else snapshot.reason,
            ))
    return rows


def _fresh_rows(
    opportunities: list[OpportunityRow],
    now: datetime,
    config: BinanceExtremeFundingConfig,
) -> list[OpportunityRow]:
    if not opportunities:
        return []
    latest_timestamp = max(row.timestamp_utc for row in opportunities)
    age_seconds = (now - latest_timestamp).total_seconds()
    if age_seconds < -5 or age_seconds > config.max_snapshot_age_seconds:
        return []
    return [row for row in opportunities if row.timestamp_utc == latest_timestamp]


def _row_for(
    rows: list[OpportunityRow],
    position: PaperPosition,
    notional_usd: float,
) -> OpportunityRow | None:
    candidates = [
        row for row in rows
        if row.base == position.base
        and row.direction == position.direction
        and abs(row.notional_usd - notional_usd) < 0.01
        and row.round_trip_fillable
    ]
    return max(candidates, key=lambda row: row.timestamp_utc, default=None)


def _mark_row_for(
    rows: list[OpportunityRow],
    position: PaperPosition,
) -> OpportunityRow | None:
    exact = _row_for(rows, position, position.notional_usd)
    if exact is not None:
        return exact
    candidates = [
        row for row in rows
        if row.base == position.base
        and row.direction == position.direction
        and row.notional_usd <= position.notional_usd
        and row.round_trip_fillable
        and row.spot_exit_avg_price is not None
        and row.perp_exit_avg_price is not None
    ]
    return max(candidates, key=lambda row: row.notional_usd, default=None)


def _basis_improvement(position: PaperPosition) -> float:
    if position.direction == "LONG_SPOT_SHORT_PERP":
        return position.entry_basis_pct - position.current_basis_pct
    return position.current_basis_pct - position.entry_basis_pct


def _estimate_exit(
    position: PaperPosition,
    row: OpportunityRow,
    notional_usd: float,
    config: BinanceExtremeFundingConfig,
) -> dict[str, float] | None:
    if (
        position.notional_usd <= 0
        or position.spot_qty <= 0
        or position.perp_qty <= 0
        or row.spot_exit_avg_price is None
        or row.perp_exit_avg_price is None
    ):
        return None
    ratio = min(1.0, notional_usd / position.notional_usd)
    spot_qty = position.spot_qty * ratio
    perp_qty = position.perp_qty * ratio
    if position.direction == "LONG_SPOT_SHORT_PERP":
        spot_pnl = spot_qty * (row.spot_exit_avg_price - position.spot_entry_price)
        perp_pnl = perp_qty * (position.perp_entry_price - row.perp_exit_avg_price)
    else:
        spot_pnl = spot_qty * (position.spot_entry_price - row.spot_exit_avg_price)
        perp_pnl = perp_qty * (row.perp_exit_avg_price - position.perp_entry_price)
    basis_pnl = spot_pnl + perp_pnl
    entry_fees = position.entry_fees_usd * ratio
    exit_fees = notional_usd * (config.estimated_exit_fee_pct + config.safety_buffer_pct) / 100
    funding_pnl = position.realised_funding_pnl_usd * ratio
    net_ex_funding = basis_pnl - entry_fees - exit_fees
    total_net = net_ex_funding + funding_pnl
    return {
        "ratio": ratio, "basis_pnl_usd": basis_pnl, "entry_fees_usd": entry_fees,
        "exit_fees_usd": exit_fees, "funding_pnl_usd": funding_pnl,
        "net_ex_funding_usd": net_ex_funding, "total_net_usd": total_net,
        "net_ex_funding_pct": net_ex_funding / notional_usd * 100,
        "total_net_pct": total_net / notional_usd * 100,
    }


def _capture_funding(
    position: PaperPosition,
    snapshot: FundingSnapshot | None,
    client: BinancePublicClient,
    store: PaperStore,
    config: BinanceExtremeFundingConfig,
    now: datetime,
) -> int:
    captures = 0
    while position.funding_time_utc is not None and position.funding_time_utc <= now:
        funding_time = position.funding_time_utc
        actual = client.fetch_settled_rate(position.perp_symbol, funding_time)
        if actual is None:
            break
        benefit = benefit_for_direction(position.direction, actual)
        funding_pnl_usd = position.notional_usd * benefit / 100
        position.actual_funding_rate_pct = actual
        position.realised_funding_pnl_usd += funding_pnl_usd
        position.funding_events_captured += 1
        position.funding_pnl_pct = (
            position.realised_funding_pnl_usd / position.notional_usd * 100
            if position.notional_usd > 0 else 0.0
        )
        store.append_funding({
            "timestamp_utc": iso(now), "position_id": position.position_id,
            "event_key": position.event_key, "perp_symbol": position.perp_symbol,
            "funding_time_utc": iso(funding_time),
            "displayed_rate_pct": position.displayed_rate_at_entry_pct,
            "actual_rate_pct": actual, "funding_benefit_pct": benefit,
            "funding_pnl_usd": funding_pnl_usd,
        })
        captures += 1
        interval = position.funding_interval_hours or config.fallback_funding_interval_hours
        position.funding_time_utc = funding_time + timedelta(hours=interval)
    if (
        snapshot is not None
        and snapshot.next_funding_time_utc is not None
        and (position.funding_time_utc is None or snapshot.next_funding_time_utc > position.funding_time_utc)
    ):
        position.funding_time_utc = snapshot.next_funding_time_utc
        position.funding_interval_hours = snapshot.funding_interval_hours or position.funding_interval_hours
    return captures


def _close_position_chunk(
    *,
    position: PaperPosition,
    row: OpportunityRow,
    estimate: dict[str, float],
    notional_usd: float,
    reason: str,
    store: PaperStore,
    config: BinanceExtremeFundingConfig,
    now: datetime,
) -> bool:
    ratio = estimate["ratio"]
    full_close = ratio >= 1 - 1e-9
    position.realised_pnl_usd += estimate["total_net_usd"]
    store.append_fill({
        "timestamp_utc": iso(now), "event_type": "EXIT" if full_close else "PARTIAL_EXIT",
        "position_id": position.position_id, "event_key": position.event_key,
        "perp_symbol": position.perp_symbol, "direction": position.direction,
        "layer_index": position.layer_index, "notional_usd": notional_usd,
        "basis_pct": position.current_basis_pct,
        "funding_rate_pct": position.actual_funding_rate_pct,
        "net_pnl_pct": estimate["total_net_pct"],
        "realised_pnl_usd": estimate["total_net_usd"], "reason": reason,
    })
    if full_close:
        position.status = "CLOSED"
        position.exit_at_utc = now
        position.exit_reason = reason
        position.estimated_net_pnl_pct = estimate["total_net_pct"]
        store.append_cooldown({
            "timestamp_utc": iso(now), "base": position.base, "direction": position.direction,
            "reason": "post_close", "expires_at_utc": iso(
                now + timedelta(minutes=config.post_close_cooldown_minutes)
            ),
        })
        return True
    remaining = 1 - ratio
    position.notional_usd *= remaining
    position.spot_qty *= remaining
    position.perp_qty *= remaining
    position.entry_fees_usd *= remaining
    position.realised_funding_pnl_usd *= remaining
    position.funding_pnl_pct = (
        position.realised_funding_pnl_usd / position.notional_usd * 100
        if position.notional_usd > 0 else 0.0
    )
    position.exit_reason = reason
    return False


def _try_profitable_exit(
    *,
    position: PaperPosition,
    rows: list[OpportunityRow],
    reason: str,
    allow_funding_harvest: bool,
    allow_partial: bool = True,
    prefer_partial: bool = False,
    allow_full: bool = True,
    store: PaperStore,
    config: BinanceExtremeFundingConfig,
    now: datetime,
) -> tuple[bool, bool]:
    def try_full() -> tuple[bool, bool]:
        if not allow_full:
            return False, False
        full_row = _row_for(rows, position, position.notional_usd)
        if full_row is None:
            return False, False
        estimate = _estimate_exit(position, full_row, position.notional_usd, config)
        if estimate is None or estimate["net_ex_funding_pct"] < config.full_exit_min_profit_pct:
            return False, False
        closed = _close_position_chunk(
            position=position, row=full_row, estimate=estimate,
            notional_usd=position.notional_usd, reason=reason, store=store,
            config=config, now=now,
        )
        return True, closed

    if not prefer_partial:
        changed, closed = try_full()
        if changed:
            return changed, closed

    if allow_partial:
        profitable_chunks: list[tuple[float, float, OpportunityRow, dict[str, float]]] = []
        for notional in sorted(config.gentle_unwind_chunk_ladder_usd):
            if notional >= position.notional_usd:
                continue
            row = _row_for(rows, position, notional)
            estimate = None if row is None else _estimate_exit(position, row, notional, config)
            if estimate is not None and estimate["net_ex_funding_pct"] >= config.full_exit_min_profit_pct:
                profitable_chunks.append((estimate["net_ex_funding_pct"], notional, row, estimate))
        if profitable_chunks:
            _, notional, row, estimate = max(profitable_chunks, key=lambda item: (item[0], item[1]))
            _close_position_chunk(
                position=position, row=row, estimate=estimate, notional_usd=notional,
                reason=f"{reason}_partial", store=store, config=config, now=now,
            )
            return True, False

    if prefer_partial:
        changed, closed = try_full()
        if changed:
            return changed, closed

    if allow_funding_harvest:
        notional = min(config.funding_harvest_unwind_chunk_usd, position.notional_usd)
        row = _row_for(rows, position, notional)
        estimate = None if row is None else _estimate_exit(position, row, notional, config)
        if estimate is not None and estimate["total_net_usd"] >= config.min_funding_harvest_profit_usd:
            closed = _close_position_chunk(
                position=position, row=row, estimate=estimate, notional_usd=notional,
                reason=f"{reason}_funding_harvest", store=store, config=config, now=now,
            )
            return True, closed
    return False, False


def _mark_and_manage_positions(
    *,
    positions: list[PaperPosition],
    snapshots: list[FundingSnapshot],
    rows: list[OpportunityRow],
    store: PaperStore,
    client: BinancePublicClient,
    config: BinanceExtremeFundingConfig,
    now: datetime,
) -> tuple[int, int, int]:
    latest = {snapshot.perp_symbol: snapshot for snapshot in snapshots}
    captures = exits = partial_exits = 0
    for position in positions:
        if position.status != "OPEN":
            continue
        snapshot = latest.get(position.perp_symbol)
        captures += _capture_funding(position, snapshot, client, store, config, now)
        full_row = _mark_row_for(rows, position)
        if full_row is not None and full_row.spot_exit_avg_price and full_row.perp_exit_avg_price:
            position.current_basis_pct = (full_row.perp_exit_avg_price / full_row.spot_exit_avg_price - 1) * 100
            estimate = _estimate_exit(position, full_row, position.notional_usd, config)
            if estimate is not None:
                position.basis_pnl_pct = estimate["basis_pnl_usd"] / position.notional_usd * 100
                position.estimated_net_pnl_pct = estimate["total_net_pct"]
        position.updated_at_utc = now

        if position.funding_events_captured == 0:
            if _basis_improvement(position) >= config.basis_take_profit_pct:
                changed, closed = _try_profitable_exit(
                    position=position, rows=rows, reason="prefunding_basis_take_profit",
                    allow_funding_harvest=False, allow_partial=True, prefer_partial=True,
                    allow_full=position.notional_usd <= max(config.gentle_unwind_chunk_ladder_usd),
                    store=store, config=config, now=now,
                )
                if changed:
                    exits += int(closed)
                    partial_exits += int(not closed)
                else:
                    position.exit_reason = "prefunding_exit_wanted_not_profitable_or_fillable"
            else:
                position.exit_reason = "hold_until_first_funding"
            continue
        current_rate = snapshot.current_funding_rate_pct if snapshot is not None else None
        next_benefit = (
            benefit_for_direction(position.direction, current_rate)
            if current_rate is not None else None
        )
        if next_benefit is not None and next_benefit >= config.juicy_hold_funding_rate_pct:
            position.exit_reason = "hold_for_juicy_next_funding"
            continue

        improvement = _basis_improvement(position)
        near_flat = abs(position.current_basis_pct) <= config.basis_near_flat_exit_abs_pct
        if next_benefit is None or next_benefit < config.min_hold_funding_rate_pct:
            reason, allow_harvest = "next_funding_weak", True
        elif next_benefit < config.juicy_hold_funding_rate_pct:
            reason, allow_harvest = "gentle_unwind", False
        elif improvement >= config.basis_take_profit_pct:
            reason, allow_harvest = "basis_take_profit", False
        elif near_flat:
            reason, allow_harvest = "basis_near_flat", False
        else:
            position.exit_reason = "hold"
            continue
        changed, closed = _try_profitable_exit(
            position=position, rows=rows, reason=reason, allow_funding_harvest=allow_harvest,
            store=store, config=config, now=now,
        )
        if changed:
            exits += int(closed)
            partial_exits += int(not closed)
        else:
            position.exit_reason = "exit_wanted_no_profitable_chunk"
    return captures, exits, partial_exits


def _add_layer(
    position: PaperPosition | None,
    row: OpportunityRow,
    config: BinanceExtremeFundingConfig,
    now: datetime,
) -> PaperPosition:
    notional = row.notional_usd
    spot_price = row.spot_entry_avg_price or 0.0
    perp_price = row.perp_entry_avg_price or 0.0
    spot_qty = notional / spot_price
    perp_qty = notional / perp_price
    entry_fee = notional * (
        config.estimated_spot_taker_fee_pct + config.estimated_perp_taker_fee_pct
    ) / 100
    entry_basis = (perp_price / spot_price - 1) * 100
    if position is None:
        return PaperPosition(
            position_id=f"BN-{uuid.uuid4().hex[:12]}", event_key=row.event_key,
            base=row.base, spot_symbol=row.spot_symbol, perp_symbol=row.perp_symbol,
            direction=row.direction, layer_index=0, notional_usd=notional,
            entry_at_utc=now, updated_at_utc=now, funding_time_utc=row.funding_time_utc,
            displayed_rate_at_entry_pct=row.funding_rate_pct or 0.0,
            actual_funding_rate_pct=None, entry_basis_pct=entry_basis,
            current_basis_pct=entry_basis, basis_pnl_pct=0.0, funding_pnl_pct=0.0,
            estimated_net_pnl_pct=row.expected_edge_pct or 0.0, realised_pnl_usd=0.0,
            status="OPEN", exit_at_utc=None, exit_reason="",
            spot_qty=spot_qty, perp_qty=perp_qty, spot_entry_price=spot_price,
            perp_entry_price=perp_price, entry_fees_usd=entry_fee,
            realised_funding_pnl_usd=0.0, funding_events_captured=0,
            funding_interval_hours=row.funding_interval_hours, last_layer_at_utc=now,
        )
    old_spot_qty, old_perp_qty = position.spot_qty, position.perp_qty
    old_notional = position.notional_usd
    position.spot_entry_price = (
        position.spot_entry_price * old_spot_qty + spot_price * spot_qty
    ) / (old_spot_qty + spot_qty)
    position.perp_entry_price = (
        position.perp_entry_price * old_perp_qty + perp_price * perp_qty
    ) / (old_perp_qty + perp_qty)
    position.spot_qty += spot_qty
    position.perp_qty += perp_qty
    position.notional_usd += notional
    position.entry_fees_usd += entry_fee
    position.entry_basis_pct = (
        position.entry_basis_pct * old_notional + entry_basis * notional
    ) / position.notional_usd
    position.displayed_rate_at_entry_pct = (
        position.displayed_rate_at_entry_pct * old_notional + (row.funding_rate_pct or 0.0) * notional
    ) / position.notional_usd
    position.layer_index += 1
    position.event_key = row.event_key
    position.funding_time_utc = row.funding_time_utc or position.funding_time_utc
    position.funding_interval_hours = row.funding_interval_hours or position.funding_interval_hours
    position.last_layer_at_utc = now
    position.updated_at_utc = now
    position.exit_reason = ""
    return position


def _open_layers(
    *,
    positions: list[PaperPosition],
    rows: list[OpportunityRow],
    signals: dict[str, dict],
    store: PaperStore,
    config: BinanceExtremeFundingConfig,
    now: datetime,
) -> int:
    open_positions = [position for position in positions if position.status == "OPEN"]
    active_cooldowns = store.load_active_cooldowns(now)
    total_open = sum(position.notional_usd for position in open_positions)
    symbol_open = {
        symbol: sum(position.notional_usd for position in open_positions if position.perp_symbol == symbol)
        for symbol in {position.perp_symbol for position in open_positions}
    }
    candidates: list[OpportunityRow] = []
    for row in rows:
        if row.decision != "ENTER_CANDIDATE" or not row.round_trip_fillable:
            continue
        current = next(
            (position for position in open_positions if position.base == row.base and position.direction == row.direction),
            None,
        )
        next_index = 0 if current is None else current.layer_index + 1
        if next_index >= len(config.layer_ladder_usd):
            continue
        if abs(row.notional_usd - config.layer_ladder_usd[next_index]) < 0.01:
            candidates.append(row)
    candidates.sort(key=lambda row: (abs(row.funding_rate_pct or 0.0), row.expected_edge_pct or 0.0), reverse=True)

    opened = 0
    handled: set[tuple[str, str]] = set()
    for row in candidates:
        key = (row.base, row.direction)
        if key in handled:
            continue
        handled.add(key)
        signal = signals.get(row.event_key)
        reason = "layer_allowed"
        allowed = True
        if signal is None or signal.get("status") != "ACTIVE":
            allowed, reason = False, "signal_not_active"
        elif parse_int(signal.get("observations")) < config.min_consistent_observations:
            allowed, reason = False, "insufficient_observations"
        else:
            first_seen = parse_datetime(signal.get("first_seen_utc")) or now
            if (now - first_seen).total_seconds() / 60 < config.min_signal_age_minutes:
                allowed, reason = False, "signal_too_new"
        current = next(
            (position for position in open_positions if position.base == row.base and position.direction == row.direction),
            None,
        )
        if allowed and key in active_cooldowns:
            allowed, reason = False, "cooldown_active"
        if allowed and current is not None and current.last_layer_at_utc is not None:
            elapsed = (now - current.last_layer_at_utc).total_seconds() / 60
            if elapsed < config.min_layer_interval_minutes:
                allowed, reason = False, "layer_interval"
        if (
            allowed
            and current is not None
            and current.exit_reason.startswith("prefunding_basis_take_profit")
        ):
            allowed, reason = False, "controlled_prefunding_exit_in_progress"
        volatile = (
            (row.basis_std_pct is not None and row.basis_std_pct > config.max_basis_std_pct)
            or (row.basis_trend_pct is not None and abs(row.basis_trend_pct) > config.max_basis_abs_trend_pct)
        )
        if allowed and volatile:
            allowed, reason = False, "basis_volatility_cooldown"
            store.append_cooldown({
                "timestamp_utc": iso(now), "base": row.base, "direction": row.direction,
                "reason": reason, "expires_at_utc": iso(
                    now + timedelta(minutes=config.volatility_cooldown_minutes)
                ),
            })
        if allowed and any(position.base == row.base and position.direction != row.direction for position in open_positions):
            allowed, reason = False, "opposite_direction_open"
        if allowed and current is None and len(open_positions) >= config.max_open_positions:
            allowed, reason = False, "max_open_positions"
        if allowed and total_open + row.notional_usd > config.max_total_notional_usd:
            allowed, reason = False, "max_total_notional"
        if allowed and symbol_open.get(row.perp_symbol, 0.0) + row.notional_usd > config.max_symbol_notional_usd:
            allowed, reason = False, "max_symbol_notional"
        store.append_decision({
            "timestamp_utc": iso(now), "decision": "ENTRY", "event_key": row.event_key,
            "perp_symbol": row.perp_symbol, "allowed": str(allowed), "reason": reason,
            "layer_index": 0 if current is None else current.layer_index + 1,
            "notional_usd": row.notional_usd,
        })
        if not allowed:
            continue
        position = _add_layer(current, row, config, now)
        if current is None:
            positions.append(position)
            open_positions.append(position)
        total_open += row.notional_usd
        symbol_open[row.perp_symbol] = symbol_open.get(row.perp_symbol, 0.0) + row.notional_usd
        opened += 1
        store.append_fill({
            "timestamp_utc": iso(now), "event_type": "ENTRY", "position_id": position.position_id,
            "event_key": row.event_key, "perp_symbol": row.perp_symbol,
            "direction": row.direction, "layer_index": position.layer_index,
            "notional_usd": row.notional_usd, "basis_pct": position.entry_basis_pct,
            "funding_rate_pct": row.funding_rate_pct, "net_pnl_pct": row.expected_edge_pct,
            "realised_pnl_usd": 0.0,
            "reason": "kucoin_style_depth_checked_layer",
        })
    return opened


def run_paper_strategy_once(
    config: BinanceExtremeFundingConfig = DEFAULT_CONFIG,
    client: BinancePublicClient | None = None,
    snapshots: list[FundingSnapshot] | None = None,
    opportunities: list[OpportunityRow] | None = None,
    now: datetime | None = None,
) -> dict:
    now = now or utc_now()
    client = client or BinancePublicClient(config)
    explicit_snapshots = snapshots is not None
    snapshots = load_latest_snapshots(config) if snapshots is None else snapshots
    if opportunities is None:
        opportunities = (
            _synthetic_opportunities(snapshots, config)
            if explicit_snapshots else load_latest_opportunities(config)
        )
    rows = _fresh_rows(opportunities, now, config)
    store = PaperStore(config)
    signals = _update_signals(store, snapshots, now)
    positions = store.load_positions()
    funding_captures, exits, partial_exits = _mark_and_manage_positions(
        positions=positions, snapshots=snapshots, rows=rows, store=store,
        client=client, config=config, now=now,
    )
    opened = _open_layers(
        positions=positions, rows=rows, signals=signals, store=store, config=config, now=now,
    )
    store.write_positions(positions)
    return {
        "snapshots": len(snapshots),
        "opportunities": len(rows),
        "active_signals": sum(row.get("status") == "ACTIVE" for row in signals.values()),
        "opened": opened, "exits": exits, "partial_exits": partial_exits,
        "funding_captures": funding_captures,
        "open_positions": sum(position.status == "OPEN" for position in positions),
    }
