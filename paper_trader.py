#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_INPUT_DIR = REPO_ROOT / "data" / "validated_futures_futures_snapshots"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "data" / "paper_trades"


def parse_bool(value) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes", "y"}


def parse_float(value, default: Optional[float] = None) -> Optional[float]:
    if value in (None, ""):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_dt(value) -> Optional[datetime]:
    if value in (None, ""):
        return None

    value = str(value).strip()
    if not value:
        return None

    if value.endswith("Z"):
        value = value[:-1] + "+00:00"

    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return dt.astimezone(timezone.utc)


def fmt_dt(dt: Optional[datetime]) -> str:
    return "" if dt is None else dt.isoformat()


def latest_validated_file() -> Path:
    files = sorted(DEFAULT_INPUT_DIR.glob("validated_futures_futures_*.csv"))
    if not files:
        raise SystemExit(f"No validated futures-futures CSV files found in {DEFAULT_INPUT_DIR}")
    return files[-1]


def candidate_key(row: dict) -> str:
    return f"{row['symbol']}|{row['direction']}|{int(float(row['notional_usdt']))}"


def row_is_tradeable(row: dict, include_spread_ready: bool = False) -> bool:
    if parse_bool(row.get("paper_ready")):
        return True
    if include_spread_ready and parse_bool(row.get("spread_ready")):
        return True
    return False


def row_passes_entry_filters(row: dict, args: argparse.Namespace) -> bool:
    if not row_is_tradeable(row, include_spread_ready=args.include_spread_ready):
        return False

    if row.get("instrument_class") != "crypto" and not args.allow_non_crypto:
        return False

    notional = parse_float(row.get("notional_usdt"))
    if notional is None or notional > args.max_notional_usdt:
        return False

    validated_spread = parse_float(row.get("validated_spread_pct"))
    if validated_spread is None or validated_spread < args.min_entry_spread_pct:
        return False

    net_edge = parse_float(row.get("net_edge_inc_funding_pct"))
    if net_edge is None or net_edge < args.min_entry_net_edge_pct:
        return False

    return True


def signed_funding_pnl(row: dict, notional_usdt: float) -> float:
    long_funding_pct = parse_float(row.get("long_funding_pct"), 0.0) or 0.0
    short_funding_pct = parse_float(row.get("short_funding_pct"), 0.0) or 0.0

    # Long perp pays positive funding and receives negative funding.
    # Short perp receives positive funding and pays negative funding.
    return notional_usdt * ((short_funding_pct - long_funding_pct) / 100)


@dataclass
class Position:
    key: str
    symbol: str
    instrument_class: str
    notional_usdt: float
    long_exchange: str
    short_exchange: str
    direction: str
    entry_time: datetime
    entry_long_price: float
    entry_short_price: float
    entry_net_edge_inc_funding_pct: float
    entry_fee_pct: float
    entry_fee_usdt: float
    long_qty: float
    short_qty: float
    funding_pnl_usdt: float = 0.0
    hold_scans: int = 0
    missing_scans: int = 0
    last_seen_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_long_price: float = 0.0
    last_short_price: float = 0.0
    applied_funding_events: set[str] = field(default_factory=set)

    def mark_to_market(self, row: dict) -> dict:
        long_price = parse_float(row.get("long_avg_price"))
        short_price = parse_float(row.get("short_avg_price"))
        if long_price is None or short_price is None:
            raise ValueError(f"Cannot mark {self.key}; missing current prices")

        long_pnl = self.long_qty * (long_price - self.entry_long_price)
        short_pnl = self.short_qty * (self.entry_short_price - short_price)
        gross_pnl = long_pnl + short_pnl
        return {
            "long_price": long_price,
            "short_price": short_price,
            "long_pnl_usdt": long_pnl,
            "short_pnl_usdt": short_pnl,
            "gross_pnl_usdt": gross_pnl,
        }


def load_rows(path: Path) -> list[dict]:
    with path.open("r", newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    rows = [row for row in rows if parse_dt(row.get("timestamp_utc")) is not None]
    rows.sort(key=lambda row: parse_dt(row["timestamp_utc"]))
    return rows


def build_position(row: dict, entry_fee_pct: float) -> Position:
    timestamp = parse_dt(row["timestamp_utc"])
    notional = float(row["notional_usdt"])
    long_price = parse_float(row.get("long_avg_price"))
    short_price = parse_float(row.get("short_avg_price"))

    if timestamp is None or long_price is None or short_price is None:
        raise ValueError(f"Cannot enter {candidate_key(row)}; missing timestamp or prices")

    return Position(
        key=candidate_key(row),
        symbol=row["symbol"],
        instrument_class=row.get("instrument_class", ""),
        notional_usdt=notional,
        long_exchange=row["long_exchange"],
        short_exchange=row["short_exchange"],
        direction=row["direction"],
        entry_time=timestamp,
        entry_long_price=long_price,
        entry_short_price=short_price,
        entry_net_edge_inc_funding_pct=parse_float(row.get("net_edge_inc_funding_pct"), 0.0) or 0.0,
        entry_fee_pct=entry_fee_pct,
        entry_fee_usdt=notional * (entry_fee_pct / 100),
        long_qty=notional / long_price,
        short_qty=notional / short_price,
        last_seen_time=timestamp,
        last_long_price=long_price,
        last_short_price=short_price,
    )


def maybe_apply_funding(position: Position, row: dict, timestamp: datetime) -> float:
    applied = 0.0

    for leg in ("long", "short"):
        event_time = parse_dt(row.get(f"{leg}_next_funding_time_utc"))
        if event_time is None or timestamp < event_time:
            continue

        event_key = f"{leg}|{event_time.isoformat()}"
        if event_key in position.applied_funding_events:
            continue

        position.applied_funding_events.add(event_key)

        funding_pct = parse_float(row.get(f"{leg}_funding_pct"), 0.0) or 0.0
        if leg == "long":
            applied -= position.notional_usdt * (funding_pct / 100)
        else:
            applied += position.notional_usdt * (funding_pct / 100)

    position.funding_pnl_usdt += applied
    return applied


def close_trade(
    *,
    position: Position,
    row: Optional[dict],
    exit_time: datetime,
    exit_reason: str,
    exit_fee_pct: float,
) -> dict:
    if row is not None:
        mark = position.mark_to_market(row)
        exit_long_price = mark["long_price"]
        exit_short_price = mark["short_price"]
        long_pnl = mark["long_pnl_usdt"]
        short_pnl = mark["short_pnl_usdt"]
        gross_pnl = mark["gross_pnl_usdt"]
        net_edge_at_exit = parse_float(row.get("net_edge_inc_funding_pct"))
        paper_ready_at_exit = parse_bool(row.get("paper_ready"))
    else:
        exit_long_price = position.last_long_price
        exit_short_price = position.last_short_price
        long_pnl = position.long_qty * (exit_long_price - position.entry_long_price)
        short_pnl = position.short_qty * (position.entry_short_price - exit_short_price)
        gross_pnl = long_pnl + short_pnl
        net_edge_at_exit = None
        paper_ready_at_exit = False

    exit_fee_usdt = position.notional_usdt * (exit_fee_pct / 100)
    total_fees = position.entry_fee_usdt + exit_fee_usdt
    net_pnl = gross_pnl + position.funding_pnl_usdt - total_fees
    net_pnl_pct = (net_pnl / position.notional_usdt) * 100

    return {
        "entry_time_utc": fmt_dt(position.entry_time),
        "exit_time_utc": fmt_dt(exit_time),
        "symbol": position.symbol,
        "instrument_class": position.instrument_class,
        "notional_usdt": f"{position.notional_usdt:.2f}",
        "long_exchange": position.long_exchange,
        "short_exchange": position.short_exchange,
        "direction": position.direction,
        "entry_long_price": f"{position.entry_long_price:.12g}",
        "entry_short_price": f"{position.entry_short_price:.12g}",
        "exit_long_price": f"{exit_long_price:.12g}",
        "exit_short_price": f"{exit_short_price:.12g}",
        "entry_net_edge_inc_funding_pct": f"{position.entry_net_edge_inc_funding_pct:.8f}",
        "exit_net_edge_inc_funding_pct": "" if net_edge_at_exit is None else f"{net_edge_at_exit:.8f}",
        "paper_ready_at_exit": str(paper_ready_at_exit),
        "hold_scans": position.hold_scans,
        "long_pnl_usdt": f"{long_pnl:.8f}",
        "short_pnl_usdt": f"{short_pnl:.8f}",
        "gross_pnl_usdt": f"{gross_pnl:.8f}",
        "funding_pnl_usdt": f"{position.funding_pnl_usdt:.8f}",
        "entry_fee_usdt": f"{position.entry_fee_usdt:.8f}",
        "exit_fee_usdt": f"{exit_fee_usdt:.8f}",
        "total_fees_usdt": f"{total_fees:.8f}",
        "net_pnl_usdt": f"{net_pnl:.8f}",
        "net_pnl_pct": f"{net_pnl_pct:.8f}",
        "exit_reason": exit_reason,
    }


def run_paper_trader(args: argparse.Namespace) -> tuple[list[dict], dict]:
    input_path = Path(args.input) if args.input else latest_validated_file()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    rows = load_rows(input_path)
    if not rows:
        raise SystemExit(f"No valid scanner rows found in {input_path}")

    scans: dict[datetime, list[dict]] = {}
    for row in rows:
        timestamp = parse_dt(row["timestamp_utc"])
        scans.setdefault(timestamp, []).append(row)

    open_positions: dict[str, Position] = {}
    closed_trades: list[dict] = []
    skipped_entries = 0
    funding_events = 0

    for timestamp in sorted(scans):
        scan_rows = scans[timestamp]
        rows_by_key = {candidate_key(row): row for row in scan_rows}
        tradeable_rows = [row for row in scan_rows if row_passes_entry_filters(row, args)]
        tradeable_rows.sort(
            key=lambda row: parse_float(row.get("net_edge_inc_funding_pct"), -999.0) or -999.0,
            reverse=True,
        )

        for key, position in list(open_positions.items()):
            row = rows_by_key.get(key)

            if row is None:
                position.missing_scans += 1
                if position.missing_scans >= args.max_missing_scans:
                    closed_trades.append(
                        close_trade(
                            position=position,
                            row=None,
                            exit_time=timestamp,
                            exit_reason="stale_signal",
                            exit_fee_pct=args.exit_fee_pct,
                        )
                    )
                    del open_positions[key]
                continue

            position.missing_scans = 0
            position.hold_scans += 1
            position.last_seen_time = timestamp
            mark = position.mark_to_market(row)
            position.last_long_price = mark["long_price"]
            position.last_short_price = mark["short_price"]

            if args.apply_funding:
                applied = maybe_apply_funding(position, row, timestamp)
                if applied:
                    funding_events += 1

            exit_fee_usdt = position.notional_usdt * (args.exit_fee_pct / 100)
            net_pnl = (
                mark["gross_pnl_usdt"]
                + position.funding_pnl_usdt
                - position.entry_fee_usdt
                - exit_fee_usdt
            )
            net_pnl_pct = (net_pnl / position.notional_usdt) * 100

            exit_reason = None
            if net_pnl_pct >= args.exit_profit_buffer_pct:
                exit_reason = "profitable_convergence"
            elif net_pnl_pct <= -abs(args.stop_loss_pct):
                exit_reason = "stop_loss"
            elif position.hold_scans >= args.max_hold_scans:
                exit_reason = "max_hold_scans"
            elif not row_is_tradeable(row, include_spread_ready=args.include_spread_ready):
                exit_reason = "signal_not_ready"

            if exit_reason:
                closed_trades.append(
                    close_trade(
                        position=position,
                        row=row,
                        exit_time=timestamp,
                        exit_reason=exit_reason,
                        exit_fee_pct=args.exit_fee_pct,
                    )
                )
                del open_positions[key]

        for row in tradeable_rows:
            key = candidate_key(row)
            if key in open_positions:
                continue
            if len(open_positions) >= args.max_open_positions:
                skipped_entries += 1
                continue

            try:
                open_positions[key] = build_position(row, entry_fee_pct=args.entry_fee_pct)
            except ValueError:
                skipped_entries += 1

    final_time = max(scans)
    for key, position in list(open_positions.items()):
        final_row = None
        for row in reversed(rows):
            if candidate_key(row) == key:
                final_row = row
                break

        closed_trades.append(
            close_trade(
                position=position,
                row=final_row,
                exit_time=final_time,
                exit_reason="end_of_file",
                exit_fee_pct=args.exit_fee_pct,
            )
        )
        del open_positions[key]

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    output_path = output_dir / f"paper_trades_{timestamp}.csv"
    write_trades(output_path, closed_trades)

    summary = build_summary(
        input_path=input_path,
        output_path=output_path,
        rows=rows,
        scans=scans,
        trades=closed_trades,
        skipped_entries=skipped_entries,
        funding_events=funding_events,
    )
    return closed_trades, summary


def write_trades(path: Path, trades: list[dict]) -> None:
    fieldnames = [
        "entry_time_utc",
        "exit_time_utc",
        "symbol",
        "instrument_class",
        "notional_usdt",
        "long_exchange",
        "short_exchange",
        "direction",
        "entry_long_price",
        "entry_short_price",
        "exit_long_price",
        "exit_short_price",
        "entry_net_edge_inc_funding_pct",
        "exit_net_edge_inc_funding_pct",
        "paper_ready_at_exit",
        "hold_scans",
        "long_pnl_usdt",
        "short_pnl_usdt",
        "gross_pnl_usdt",
        "funding_pnl_usdt",
        "entry_fee_usdt",
        "exit_fee_usdt",
        "total_fees_usdt",
        "net_pnl_usdt",
        "net_pnl_pct",
        "exit_reason",
    ]

    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(trades)


def build_summary(
    *,
    input_path: Path,
    output_path: Path,
    rows: list[dict],
    scans: dict[datetime, list[dict]],
    trades: list[dict],
    skipped_entries: int,
    funding_events: int,
) -> dict:
    net_pnls = [float(trade["net_pnl_usdt"]) for trade in trades]
    wins = [pnl for pnl in net_pnls if pnl > 0]
    losses = [pnl for pnl in net_pnls if pnl < 0]

    return {
        "input_path": input_path,
        "output_path": output_path,
        "scanner_rows": len(rows),
        "scans": len(scans),
        "closed_trades": len(trades),
        "winning_trades": len(wins),
        "losing_trades": len(losses),
        "win_rate_pct": (len(wins) / len(trades) * 100) if trades else 0.0,
        "net_pnl_usdt": sum(net_pnls),
        "avg_trade_pnl_usdt": (sum(net_pnls) / len(trades)) if trades else 0.0,
        "best_trade_usdt": max(net_pnls) if net_pnls else 0.0,
        "worst_trade_usdt": min(net_pnls) if net_pnls else 0.0,
        "skipped_entries": skipped_entries,
        "funding_events": funding_events,
    }


def print_summary(summary: dict, trades: list[dict]) -> None:
    print(f"Input: {summary['input_path']}")
    print(f"Output: {summary['output_path']}")
    print(f"Scanner rows: {summary['scanner_rows']}")
    print(f"Scans: {summary['scans']}")
    print(f"Closed trades: {summary['closed_trades']}")
    print(f"Win rate: {summary['win_rate_pct']:.2f}%")
    print(f"Net PnL: ${summary['net_pnl_usdt']:.4f}")
    print(f"Average trade PnL: ${summary['avg_trade_pnl_usdt']:.4f}")
    print(f"Best trade: ${summary['best_trade_usdt']:.4f}")
    print(f"Worst trade: ${summary['worst_trade_usdt']:.4f}")
    print(f"Skipped entries: {summary['skipped_entries']}")
    print(f"Funding events applied: {summary['funding_events']}")

    if not trades:
        return

    print("\nTop trades by net PnL")
    top = sorted(trades, key=lambda trade: float(trade["net_pnl_usdt"]), reverse=True)[:10]
    for trade in top:
        print(
            f"{trade['symbol']:<12} "
            f"{trade['direction']:<29} "
            f"${float(trade['notional_usdt']):>8,.0f} "
            f"pnl=${float(trade['net_pnl_usdt']):>9.4f} "
            f"({float(trade['net_pnl_pct']):>7.4f}%) "
            f"exit={trade['exit_reason']}"
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Paper trade validated futures-futures scanner opportunities.",
    )
    parser.add_argument(
        "--input",
        help="Validated scanner CSV. Defaults to latest data/validated_futures_futures_snapshots file.",
    )
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_DIR),
        help="Directory for paper trade CSV output.",
    )
    parser.add_argument(
        "--max-open-positions",
        type=int,
        default=8,
        help="Maximum simultaneous paper positions.",
    )
    parser.add_argument(
        "--max-notional-usdt",
        type=float,
        default=5_000,
        help="Maximum notional per leg.",
    )
    parser.add_argument(
        "--entry-fee-pct",
        type=float,
        default=0.10,
        help="Opening fee percentage for both legs combined.",
    )
    parser.add_argument(
        "--exit-fee-pct",
        type=float,
        default=0.10,
        help="Closing fee percentage for both legs combined.",
    )
    parser.add_argument(
        "--min-entry-spread-pct",
        type=float,
        default=1.50,
        help="Only enter when validated spread is at least this percentage.",
    )
    parser.add_argument(
        "--min-entry-net-edge-pct",
        type=float,
        default=0.0,
        help="Only enter when scanner net edge including funding is at least this percentage.",
    )
    parser.add_argument(
        "--exit-profit-buffer-pct",
        type=float,
        default=0.05,
        help="Close when net mark-to-market PnL after exit fees reaches this percentage of notional.",
    )
    parser.add_argument(
        "--stop-loss-pct",
        type=float,
        default=0.20,
        help="Close when net PnL falls by this percentage of notional.",
    )
    parser.add_argument(
        "--max-hold-scans",
        type=int,
        default=8,
        help="Close after this many matching scan updates.",
    )
    parser.add_argument(
        "--max-missing-scans",
        type=int,
        default=2,
        help="Close after a position is absent from this many scans.",
    )
    parser.add_argument(
        "--include-spread-ready",
        action="store_true",
        help="Allow spread_ready rows even when paper_ready is false.",
    )
    parser.add_argument(
        "--allow-non-crypto",
        action="store_true",
        help="Allow tokenised/synthetic instruments.",
    )
    parser.add_argument(
        "--apply-funding",
        action="store_true",
        help="Apply funding PnL when a scan timestamp reaches a leg's next funding time.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    trades, summary = run_paper_trader(args)
    print_summary(summary, trades)


if __name__ == "__main__":
    main()
