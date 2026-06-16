from __future__ import annotations

import argparse
import csv
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from strategy.config import DEFAULT_CONFIG, StrategyConfig
from strategy.entry_rules import evaluate_entry, evaluate_funding_capture_ready
from strategy.exit_rules import calculate_take_profit_pct, evaluate_exit
from strategy.models import ValidatedOpportunity, format_datetime
from strategy.paper_execution import PaperExecutionEngine
from strategy.position_store import CsvPositionStore


def latest_validated_file(config: StrategyConfig) -> Path:
    files = sorted(config.validated_input_dir.glob("validated_futures_futures_*.csv"))
    if not files:
        raise SystemExit(f"No validated scanner files found in {config.validated_input_dir}")
    return files[-1]


def load_opportunities(path: Path) -> list[ValidatedOpportunity]:
    with path.open("r", newline="", encoding="utf-8") as f:
        rows = csv.DictReader(f)
        opportunities = []
        for row in rows:
            try:
                opportunities.append(ValidatedOpportunity.from_csv_row(row))
            except ValueError:
                continue
    opportunities.sort(key=lambda item: item.timestamp_utc)
    return opportunities


def choose_best_rows(rows: list[ValidatedOpportunity]) -> list[ValidatedOpportunity]:
    by_position: dict[str, ValidatedOpportunity] = {}
    for opportunity in sorted(
        rows,
        key=lambda item: (
            item.net_edge_inc_funding_pct if item.net_edge_inc_funding_pct is not None else -999,
            item.notional_usdt,
        ),
        reverse=True,
    ):
        by_position.setdefault(opportunity.position_key, opportunity)
    return list(by_position.values())


def choose_best_entry_rows(
    rows: list[ValidatedOpportunity],
    config: StrategyConfig,
) -> list[ValidatedOpportunity]:
    grouped: dict[str, list[ValidatedOpportunity]] = defaultdict(list)
    for row in rows:
        grouped[row.position_key].append(row)

    selected = []
    for position_rows in grouped.values():
        notional_rows = [
            row
            for row in position_rows
            if config.min_validated_notional_usd <= row.notional_usdt <= config.max_slice_notional_usd
        ]
        if not notional_rows:
            notional_rows = position_rows

        best = max(
            notional_rows,
            key=lambda item: (
                item.net_edge_ex_funding_pct if item.net_edge_ex_funding_pct is not None else -999,
                item.validated_spread_pct if item.validated_spread_pct is not None else -999,
                item.route_spread_percentile if item.route_spread_percentile is not None else -999,
                item.route_spread_zscore if item.route_spread_zscore is not None else -999,
                item.net_edge_inc_funding_pct if item.net_edge_inc_funding_pct is not None else -999,
                -abs(config.max_slice_notional_usd - item.notional_usdt)
                if item.notional_usdt <= config.max_slice_notional_usd
                else -999_999 - item.notional_usdt,
            ),
        )
        selected.append(best)

    selected.sort(
        key=lambda item: (
            item.net_edge_ex_funding_pct if item.net_edge_ex_funding_pct is not None else -999,
            item.validated_spread_pct if item.validated_spread_pct is not None else -999,
            item.route_spread_percentile if item.route_spread_percentile is not None else -999,
            item.route_spread_zscore if item.route_spread_zscore is not None else -999,
            item.net_edge_inc_funding_pct if item.net_edge_inc_funding_pct is not None else -999,
        ),
        reverse=True,
    )
    return selected


def decision_funding_context(
    opportunity: ValidatedOpportunity | None,
    config: StrategyConfig,
    now: datetime,
) -> dict:
    if opportunity is None:
        return {
            "funding_benefit_pct": None,
            "min_minutes_to_funding": None,
            "funding_capture_ready": None,
        }
    return {
        "funding_benefit_pct": opportunity.funding_benefit_pct,
        "min_minutes_to_funding": opportunity.min_minutes_to_funding(now),
        "funding_capture_ready": evaluate_funding_capture_ready(opportunity, config, now),
    }


def decision_optimisation_context(
    opportunity: ValidatedOpportunity | None,
    config: StrategyConfig,
) -> dict:
    context = {
        "validated_spread_pct": None,
        "net_edge_ex_funding_pct": None,
        "net_edge_inc_funding_pct": None,
        "fast_spread_pct": None,
        "slippage_pct": None,
        "close_slippage_pct": None,
        "route_observation_count": None,
        "route_spread_percentile": None,
        "route_spread_zscore": None,
        "route_spread_trend_pct": None,
        "route_spread_mean_pct": None,
        "route_spread_median_pct": None,
        "config_max_slice_notional_usd": config.max_slice_notional_usd,
        "config_max_slices_per_position": config.max_slices_per_position,
        "config_min_route_spread_percentile": config.min_route_spread_percentile,
        "config_min_route_spread_zscore": config.min_route_spread_zscore,
        "config_max_route_spread_trend_pct": config.max_route_spread_trend_pct,
        "config_min_validated_spread_pct": config.min_validated_spread_pct,
        "config_min_net_spread_ex_funding_pct": config.min_net_spread_ex_funding_pct,
        "config_min_net_edge_inc_funding_pct": config.min_net_edge_inc_funding_pct,
        "config_spread_compression_exit_pct": config.spread_compression_exit_pct,
        "config_min_take_profit_pct": config.min_take_profit_pct,
        "config_take_profit_edge_fraction": config.take_profit_edge_fraction,
        "config_max_take_profit_pct": config.max_take_profit_pct,
    }
    if opportunity is None:
        return context

    context.update(
        {
            "validated_spread_pct": opportunity.validated_spread_pct,
            "net_edge_ex_funding_pct": opportunity.net_edge_ex_funding_pct,
            "net_edge_inc_funding_pct": opportunity.net_edge_inc_funding_pct,
            "fast_spread_pct": opportunity.fast_spread_pct,
            "slippage_pct": opportunity.slippage_pct,
            "close_slippage_pct": opportunity.close_slippage_pct,
            "route_observation_count": opportunity.route_observation_count,
            "route_spread_percentile": opportunity.route_spread_percentile,
            "route_spread_zscore": opportunity.route_spread_zscore,
            "route_spread_trend_pct": opportunity.route_spread_trend_pct,
            "route_spread_mean_pct": opportunity.route_spread_mean_pct,
            "route_spread_median_pct": opportunity.route_spread_median_pct,
        }
    )
    return context


def process_scan(
    *,
    scan_time: str,
    scan_rows: list[ValidatedOpportunity],
    source_file: Path,
    store: CsvPositionStore,
    engine: PaperExecutionEngine,
    config: StrategyConfig,
    positions,
) -> None:
    decision_now = datetime.now(timezone.utc)
    current_by_position = {row.position_key: row for row in choose_best_rows(scan_rows)}

    for position_id, position in list(positions.items()):
        opportunity = current_by_position.get(position_id)
        if opportunity is not None:
            position.missing_scan_count = 0
            if opportunity.long_close_fillable and opportunity.short_close_fillable:
                position.close_liquidity_warning_count = 0
            else:
                position.close_liquidity_warning_count += 1
            engine.refresh_position_marks(position, opportunity)
        else:
            position.missing_scan_count += 1

        exit_decision = evaluate_exit(
            position=position,
            opportunity=opportunity,
            config=config,
            now=scan_rows[0].timestamp_utc,
        )
        funding_context = decision_funding_context(opportunity, config, decision_now)
        optimisation_context = decision_optimisation_context(opportunity, config)
        store.append_decision(
            decision_type="EXIT",
            symbol=position.symbol,
            position_id=position.position_id,
            opportunity_key=opportunity.opportunity_key if opportunity else "",
            allowed=exit_decision.should_exit,
            reason=exit_decision.reason,
            notional_usd=position.total_notional_usd,
            estimated_net_pnl_usd=exit_decision.estimated_net_pnl_usd,
            estimated_net_pnl_pct=exit_decision.estimated_net_pnl_pct,
            entry_net_edge_pct=position.entry_net_edge_pct,
            effective_take_profit_pct=calculate_take_profit_pct(position, config),
            use_dynamic_take_profit=config.use_dynamic_take_profit,
            **funding_context,
            **optimisation_context,
        )
        if exit_decision.should_exit:
            engine.close_position(position, opportunity, exit_decision.reason)
            positions.pop(position_id, None)

    daily_risk_state = store.calculate_daily_risk_state(now=scan_rows[0].timestamp_utc)
    candidates = choose_best_entry_rows(scan_rows, config)

    for opportunity in candidates:
        funding_context = decision_funding_context(opportunity, config, decision_now)
        optimisation_context = decision_optimisation_context(opportunity, config)
        entry_decision = evaluate_entry(
            opportunity=opportunity,
            open_positions=positions,
            config=config,
            daily_entry_count=daily_risk_state["daily_entry_count"],
            daily_realised_pnl_usd=daily_risk_state["daily_realised_pnl_usd"],
            consecutive_losses=daily_risk_state["consecutive_losses"],
        )
        store.append_decision(
            decision_type="ENTRY",
            symbol=opportunity.symbol,
            position_id=opportunity.position_key,
            opportunity_key=opportunity.opportunity_key,
            allowed=entry_decision.should_enter,
            reason=entry_decision.reason,
            notional_usd=entry_decision.desired_notional_usd,
            entry_net_edge_pct=opportunity.net_edge_inc_funding_pct,
            use_dynamic_take_profit=config.use_dynamic_take_profit,
            **funding_context,
            **optimisation_context,
        )
        if entry_decision.should_enter:
            engine.open_or_add_slice(
                opportunity=opportunity,
                positions=positions,
                notional_usd=entry_decision.desired_notional_usd,
                reason=entry_decision.reason,
            )
            daily_risk_state["daily_entry_count"] += 1

    store.write_positions(positions)
    store.mark_scan_processed(scan_time, source_file)


def build_config(args: argparse.Namespace) -> StrategyConfig:
    return StrategyConfig(
        max_slice_notional_usd=args.max_slice_notional_usd,
        min_validated_notional_usd=args.min_validated_notional_usd,
        min_validated_spread_pct=args.min_validated_spread_pct,
        require_paper_ready=not args.allow_not_paper_ready,
        data_dir=Path(args.data_dir),
        validated_input_dir=Path(args.validated_input_dir),
    )


def process_available_scans(args: argparse.Namespace, config: StrategyConfig) -> dict:
    source_file = Path(args.input) if args.input else latest_validated_file(config)
    opportunities = load_opportunities(source_file)
    if not opportunities:
        return {
            "source_file": source_file,
            "scans_available": 0,
            "scans_processed": 0,
            "open_positions": 0,
        }

    grouped = defaultdict(list)
    for opportunity in opportunities:
        grouped[format_datetime(opportunity.timestamp_utc)].append(opportunity)

    store = CsvPositionStore(config)
    engine = PaperExecutionEngine(config, store)
    positions = store.load_open_positions()
    processed = store.load_processed_scans()

    scan_times = sorted(grouped.keys())
    if args.latest_only:
        scan_times = scan_times[-1:]

    processed_count = 0
    for scan_time in scan_times:
        if not args.reprocess and scan_time in processed:
            continue
        process_scan(
            scan_time=scan_time,
            scan_rows=grouped[scan_time],
            source_file=source_file,
            store=store,
            engine=engine,
            config=config,
            positions=positions,
        )
        processed_count += 1

    return {
        "source_file": source_file,
        "scans_available": len(grouped),
        "scans_processed": processed_count,
        "open_positions": len(positions),
    }


def print_summary(summary: dict, config: StrategyConfig) -> None:
    print(f"Source: {summary['source_file']}")
    print(f"Scans available: {summary['scans_available']}")
    print(f"Scans processed: {summary['scans_processed']}")
    print(f"Open positions: {summary['open_positions']}")
    print(f"Strategy data: {config.data_dir}")


def run(args: argparse.Namespace) -> None:
    config = build_config(args)

    if not args.loop:
        summary = process_available_scans(args, config)
        print_summary(summary, config)
        return

    print(f"Strategy loop running every {args.interval} seconds. Press Ctrl+C to stop.")
    try:
        while True:
            summary = process_available_scans(args, config)
            if summary["scans_processed"] or not args.quiet_idle:
                print_summary(summary, config)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nStrategy loop stopped.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run paper strategy decisions from validated futures-futures scanner CSV.")
    parser.add_argument("--input", help="Validated scanner CSV. Defaults to latest file.")
    parser.add_argument("--data-dir", default=str(DEFAULT_CONFIG.data_dir), help="CSV-backed strategy storage directory.")
    parser.add_argument(
        "--validated-input-dir",
        default=str(DEFAULT_CONFIG.validated_input_dir),
        help="Directory containing validated scanner CSV files.",
    )
    parser.add_argument("--latest-only", action="store_true", help="Only process the latest scan timestamp.")
    parser.add_argument("--reprocess", action="store_true", help="Process scan timestamps even if already marked processed.")
    parser.add_argument("--loop", action="store_true", help="Continuously poll the validated scanner CSV for new scans.")
    parser.add_argument("--interval", type=int, default=30, help="Seconds between loop polls.")
    parser.add_argument("--quiet-idle", action="store_true", help="In loop mode, only print when new scans are processed.")
    parser.add_argument("--allow-not-paper-ready", action="store_true", help="Allow rows that are not scanner paper_ready.")
    parser.add_argument("--max-slice-notional-usd", type=float, default=DEFAULT_CONFIG.max_slice_notional_usd)
    parser.add_argument("--min-validated-notional-usd", type=float, default=DEFAULT_CONFIG.min_validated_notional_usd)
    parser.add_argument("--min-validated-spread-pct", type=float, default=DEFAULT_CONFIG.min_validated_spread_pct)
    return parser.parse_args()


if __name__ == "__main__":
    run(parse_args())
