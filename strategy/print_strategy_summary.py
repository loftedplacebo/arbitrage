from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from strategy.config import DEFAULT_CONFIG, StrategyConfig
from strategy.models import parse_datetime, parse_float
from strategy.position_store import CsvPositionStore


def load_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def is_today(row: dict, field: str, now: datetime) -> bool:
    timestamp = parse_datetime(row.get(field))
    return timestamp is not None and timestamp.date() == now.astimezone(timezone.utc).date()


def print_counter(title: str, counter: Counter, limit: int = 10) -> None:
    print(f"\n{title}")
    if not counter:
        print("  none")
        return
    for reason, count in counter.most_common(limit):
        print(f"  {reason}: {count}")


def run(args: argparse.Namespace) -> None:
    config = StrategyConfig(data_dir=Path(args.data_dir))
    store = CsvPositionStore(config)
    now = datetime.now(timezone.utc)

    positions = store.load_all_positions()
    open_positions = [position for position in positions if position.status == "OPEN"]
    fills = store.load_fills()
    decisions = load_csv(store.decisions_path)

    total_open_notional = sum(position.total_notional_usd for position in open_positions)
    latest_estimated_pnl = sum(position.estimated_net_pnl for position in open_positions)
    positive_open_positions = sum(1 for position in open_positions if position.estimated_net_pnl > 0)
    negative_open_positions = sum(1 for position in open_positions if position.estimated_net_pnl < 0)
    open_liquidity_warnings = sum(
        1
        for position in open_positions
        if position.close_liquidity_warning_count > 0
    )

    by_symbol = defaultdict(float)
    for position in open_positions:
        by_symbol[position.symbol] += position.total_notional_usd

    todays_open_slices = [
        row
        for row in fills
        if row.get("event_type") == "OPEN_SLICE" and is_today(row, "timestamp_utc", now)
    ]
    todays_closes = [
        row
        for row in fills
        if row.get("event_type") == "CLOSE_POSITION" and is_today(row, "timestamp_utc", now)
    ]
    todays_realised_pnl = sum(
        parse_float(row.get("realised_pnl_usd"), 0.0) or 0.0
        for row in todays_closes
    )

    entry_rejections = Counter(
        row.get("reason", "")
        for row in decisions
        if row.get("decision_type") == "ENTRY"
        and str(row.get("allowed")).lower() == "false"
    )
    exit_reasons = Counter(
        row.get("reason", "")
        for row in decisions
        if row.get("decision_type") == "EXIT"
    )
    funding_timing_reasons = [
        "funding_negative_but_not_near_event",
        "negative_funding_near_event_losing",
        "negative_funding_near_event_material",
        "projected_negative_funding_too_high",
        "negative_funding_near_event_profit_too_small",
        "hold_negative_funding_small_profit_buffer_ok",
    ]
    funding_timing_counts = Counter({
        reason: exit_reasons.get(reason, 0)
        for reason in funding_timing_reasons
        if exit_reasons.get(reason, 0)
    })
    todays_funding_capture_entries = [
        row
        for row in decisions
        if row.get("decision_type") == "ENTRY"
        and row.get("reason") == "funding_capture_entry_ok"
        and is_today(row, "timestamp_utc", now)
    ]
    todays_funding_holds = [
        row
        for row in decisions
        if row.get("decision_type") == "EXIT"
        and row.get("reason") == "hold_for_favourable_funding"
        and is_today(row, "timestamp_utc", now)
    ]
    recent_exit_tp_context = [
        row
        for row in decisions
        if row.get("decision_type") == "EXIT"
        and row.get("effective_take_profit_pct")
    ][-5:]
    recent_capture_ready = [
        row
        for row in decisions
        if str(row.get("funding_capture_ready")).lower() == "true"
    ][-5:]

    print(f"Strategy data: {store.data_dir}")
    print(f"Open positions: {len(open_positions)}")
    print(f"Open positions with close-liquidity warnings: {open_liquidity_warnings}")
    print(f"Total open notional: ${total_open_notional:,.2f}")
    print(f"Latest estimated open PnL: ${latest_estimated_pnl:,.4f}")
    print(f"Open positions with positive estimated PnL: {positive_open_positions}")
    print(f"Open positions with negative estimated PnL: {negative_open_positions}")
    print("Funding accrual: not implemented yet; paper PnL is spread-only minus estimated fees/slippage.")

    print("\nActive config")
    print(f"  Max daily entries: {config.max_daily_entries}")
    print(f"  Max open positions: {config.max_open_positions}")
    print(f"  Max slices per position: {config.max_slices_per_position}")
    print(f"  Max total open notional: ${config.max_total_open_notional_usd:,.2f}")
    print(f"  Max symbol notional: ${config.max_symbol_notional_usd:,.2f}")
    print(f"  Max exchange notional: ${config.max_exchange_notional_usd:,.2f}")
    print(f"  Funding capture enabled: {config.funding_capture_enabled}")
    print(f"  Funding capture window minutes: {config.funding_capture_window_minutes:g}")
    print(f"  Min validated spread %: {config.min_validated_spread_pct:g}")
    print(f"  Min net spread ex funding %: {config.min_net_spread_ex_funding_pct:g}")
    print(f"  Min net edge inc funding %: {config.min_net_edge_inc_funding_pct:g}")
    print(f"  Normal entry min minutes to funding: {config.normal_entry_min_minutes_to_funding:g}")
    print(
        "  Normal entry near-funding benefit allowance %: "
        f"{config.normal_entry_allow_near_funding_if_benefit_pct:g}"
    )
    print(f"  Min funding benefit for capture %: {config.min_funding_benefit_for_capture_pct:g}")
    print(
        "  Funding capture min net spread ex funding %: "
        f"{config.funding_capture_min_net_spread_ex_funding_pct:g}"
    )
    print(
        "  Funding capture min net edge inc funding %: "
        f"{config.funding_capture_min_net_edge_inc_funding_pct:g}"
    )
    print(f"  Fixed take profit fallback %: {config.take_profit_pct:g}")
    print(f"  Dynamic take profit enabled: {config.use_dynamic_take_profit}")
    print(f"  Min take profit %: {config.min_take_profit_pct:g}")
    print(f"  Take profit edge fraction: {config.take_profit_edge_fraction:g}")
    print(f"  Max take profit %: {config.max_take_profit_pct:g}")
    print(f"  Stop loss enabled: {config.stop_loss_enabled}")
    print(f"  Stop loss pct: {config.stop_loss_pct:g}")
    print(f"  Min profit to exit remaining edge %: {config.min_profit_to_exit_remaining_edge_pct:g}")
    print(f"  Exit on missing opportunity: {config.exit_on_missing_opportunity}")
    print(f"  Max existing position loss pct for add: {config.max_existing_position_loss_pct_for_add:g}")
    print(f"  Funding exit decision window minutes: {config.funding_exit_decision_window_minutes:g}")

    print("\nOpen positions by symbol")
    if not by_symbol:
        print("  none")
    else:
        for symbol, notional in sorted(by_symbol.items()):
            print(f"  {symbol}: ${notional:,.2f}")

    print(f"\nToday's opened slices: {len(todays_open_slices)}")
    print(f"Today's closed positions: {len(todays_closes)}")
    print(f"Today's realised PnL from closed positions: ${todays_realised_pnl:,.4f}")
    if not todays_closes:
        print(
            "No positions closed today, so realised PnL is still zero. "
            "Check latest estimated open PnL for current open trade performance."
        )
    print(f"Funding capture entries today: {len(todays_funding_capture_entries)}")
    print(f"Funding holds today: {len(todays_funding_holds)}")

    print("\nRecent funding-capture-ready decisions")
    if not recent_capture_ready:
        print("  none")
    else:
        for row in recent_capture_ready:
            print(
                "  "
                f"{row.get('timestamp_utc', '')} "
                f"{row.get('symbol', '')} "
                f"{row.get('decision_type', '')} "
                f"reason={row.get('reason', '')} "
                f"benefit={row.get('funding_benefit_pct', '')} "
                f"minutes={row.get('min_minutes_to_funding', '')}"
            )

    print("\nRecent exit take-profit context")
    if not recent_exit_tp_context:
        print("  none")
    else:
        for row in recent_exit_tp_context:
            print(
                "  "
                f"{row.get('timestamp_utc', '')} "
                f"{row.get('symbol', '')} "
                f"reason={row.get('reason', '')} "
                f"entry_edge={row.get('entry_net_edge_pct', '')} "
                f"effective_tp={row.get('effective_take_profit_pct', '')} "
                f"dynamic={row.get('use_dynamic_take_profit', '')}"
            )

    print_counter("Most common entry rejection reasons", entry_rejections)
    print_counter("Most common exit reasons", exit_reasons)
    print_counter("Funding timing exit decisions", funding_timing_counts)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Print current paper strategy state.")
    parser.add_argument("--data-dir", default=str(DEFAULT_CONFIG.data_dir))
    return parser.parse_args()


if __name__ == "__main__":
    run(parse_args())
