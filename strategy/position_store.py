from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path

from strategy.config import StrategyConfig
from strategy.models import Position, PositionSlice, format_datetime, parse_datetime, parse_float, utc_now


POSITION_FIELDS = [
    "position_id",
    "symbol",
    "long_exchange",
    "short_exchange",
    "total_notional_usd",
    "slice_count",
    "average_long_entry_price",
    "average_short_entry_price",
    "entry_spread_pct",
    "current_spread_pct",
    "entry_net_edge_pct",
    "current_net_edge_pct",
    "realised_funding_pnl",
    "unrealised_spread_pnl",
    "estimated_close_cost",
    "estimated_net_pnl",
    "created_at",
    "updated_at",
    "missing_scan_count",
    "close_liquidity_warning_count",
    "status",
]

SLICE_FIELDS = [
    "slice_id",
    "position_id",
    "entry_time",
    "notional_usd",
    "long_order_id",
    "short_order_id",
    "long_fill_price",
    "short_fill_price",
    "entry_fees",
    "entry_slippage",
    "entry_reason",
    "status",
]

DECISION_FIELDS = [
    "timestamp_utc",
    "decision_type",
    "symbol",
    "position_id",
    "opportunity_key",
    "allowed",
    "reason",
    "notional_usd",
    "estimated_net_pnl_usd",
    "estimated_net_pnl_pct",
    "funding_benefit_pct",
    "min_minutes_to_funding",
    "funding_capture_ready",
    "entry_net_edge_pct",
    "effective_take_profit_pct",
    "use_dynamic_take_profit",
]

FILL_FIELDS = [
    "timestamp_utc",
    "event_type",
    "position_id",
    "slice_id",
    "symbol",
    "long_exchange",
    "short_exchange",
    "notional_usd",
    "long_price",
    "short_price",
    "fees_usd",
    "slippage_pct",
    "realised_pnl_usd",
    "reason",
]

PROCESSED_SCAN_FIELDS = ["timestamp_utc", "source_file", "processed_at_utc"]


class CsvPositionStore:
    def __init__(self, config: StrategyConfig):
        self.data_dir = Path(config.data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.positions_path = self.data_dir / "positions.csv"
        self.slices_path = self.data_dir / "slices.csv"
        self.decisions_path = self.data_dir / "decisions.csv"
        self.fills_path = self.data_dir / "fills.csv"
        self.processed_scans_path = self.data_dir / "processed_scans.csv"

    def load_open_positions(self) -> dict[str, Position]:
        return {
            position.position_id: position
            for position in self.load_all_positions()
            if position.status == "OPEN"
        }

    def load_all_positions(self) -> list[Position]:
        if not self.positions_path.exists():
            return []
        with self.positions_path.open("r", newline="", encoding="utf-8") as f:
            rows = csv.DictReader(f)
            return [Position.from_csv_row(row) for row in rows]

    def write_positions(self, positions: dict[str, Position]) -> None:
        existing_closed = [
            position
            for position in self.load_all_positions()
            if position.status != "OPEN" and position.position_id not in positions
        ]
        all_positions = existing_closed + list(positions.values())
        with self.positions_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=POSITION_FIELDS)
            writer.writeheader()
            for position in all_positions:
                writer.writerow(position.to_csv_row())

    def upsert_position(self, position: Position) -> None:
        positions = {item.position_id: item for item in self.load_all_positions()}
        positions[position.position_id] = position
        with self.positions_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=POSITION_FIELDS)
            writer.writeheader()
            for item in positions.values():
                writer.writerow(item.to_csv_row())

    def append_slice(self, position_slice: PositionSlice) -> None:
        self._append_row(self.slices_path, SLICE_FIELDS, position_slice.to_csv_row())

    def append_decision(
        self,
        *,
        decision_type: str,
        symbol: str,
        position_id: str,
        opportunity_key: str,
        allowed: bool,
        reason: str,
        notional_usd: float = 0.0,
        estimated_net_pnl_usd: float = 0.0,
        estimated_net_pnl_pct: float = 0.0,
        funding_benefit_pct: float | None = None,
        min_minutes_to_funding: float | None = None,
        funding_capture_ready: bool | None = None,
        entry_net_edge_pct: float | None = None,
        effective_take_profit_pct: float | None = None,
        use_dynamic_take_profit: bool | None = None,
    ) -> None:
        self._append_row(
            self.decisions_path,
            DECISION_FIELDS,
            {
                "timestamp_utc": format_datetime(utc_now()),
                "decision_type": decision_type,
                "symbol": symbol,
                "position_id": position_id,
                "opportunity_key": opportunity_key,
                "allowed": str(allowed),
                "reason": reason,
                "notional_usd": f"{notional_usd:.8f}",
                "estimated_net_pnl_usd": f"{estimated_net_pnl_usd:.8f}",
                "estimated_net_pnl_pct": f"{estimated_net_pnl_pct:.8f}",
                "funding_benefit_pct": "" if funding_benefit_pct is None else f"{funding_benefit_pct:.8f}",
                "min_minutes_to_funding": "" if min_minutes_to_funding is None else f"{min_minutes_to_funding:.8f}",
                "funding_capture_ready": "" if funding_capture_ready is None else str(funding_capture_ready),
                "entry_net_edge_pct": "" if entry_net_edge_pct is None else f"{entry_net_edge_pct:.8f}",
                "effective_take_profit_pct": (
                    "" if effective_take_profit_pct is None else f"{effective_take_profit_pct:.8f}"
                ),
                "use_dynamic_take_profit": "" if use_dynamic_take_profit is None else str(use_dynamic_take_profit),
            },
        )

    def append_fill(self, row: dict) -> None:
        output = {field: row.get(field, "") for field in FILL_FIELDS}
        self._append_row(self.fills_path, FILL_FIELDS, output)

    def load_fills(self) -> list[dict]:
        if not self.fills_path.exists():
            return []
        with self.fills_path.open("r", newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))

    def calculate_daily_risk_state(self, now: datetime | None = None) -> dict:
        now = now or datetime.now(timezone.utc)
        today = now.astimezone(timezone.utc).date()
        fills = self.load_fills()

        todays_fills = []
        close_events = []
        for row in fills:
            timestamp = parse_datetime(row.get("timestamp_utc"))
            if timestamp is None:
                continue
            if row.get("event_type") == "CLOSE_POSITION":
                close_events.append(row)
            if timestamp.date() == today:
                todays_fills.append(row)

        daily_entry_count = sum(1 for row in todays_fills if row.get("event_type") == "OPEN_SLICE")
        daily_realised_pnl_usd = sum(
            parse_float(row.get("realised_pnl_usd"), 0.0) or 0.0
            for row in todays_fills
            if row.get("event_type") == "CLOSE_POSITION"
        )

        consecutive_losses = 0
        for row in reversed(close_events):
            pnl = parse_float(row.get("realised_pnl_usd"), 0.0) or 0.0
            if pnl < 0:
                consecutive_losses += 1
                continue
            break

        return {
            "daily_entry_count": daily_entry_count,
            "daily_realised_pnl_usd": daily_realised_pnl_usd,
            "consecutive_losses": consecutive_losses,
        }

    def load_processed_scans(self) -> set[str]:
        if not self.processed_scans_path.exists():
            return set()
        with self.processed_scans_path.open("r", newline="", encoding="utf-8") as f:
            return {row["timestamp_utc"] for row in csv.DictReader(f)}

    def mark_scan_processed(self, timestamp_utc: str, source_file: Path) -> None:
        self._append_row(
            self.processed_scans_path,
            PROCESSED_SCAN_FIELDS,
            {
                "timestamp_utc": timestamp_utc,
                "source_file": str(source_file),
                "processed_at_utc": format_datetime(utc_now()),
            },
        )

    @staticmethod
    def _append_row(path: Path, fieldnames: list[str], row: dict) -> None:
        file_exists = path.exists()
        if file_exists:
            with path.open("r", newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                existing_fieldnames = reader.fieldnames or []
                if existing_fieldnames != fieldnames:
                    existing_rows = list(reader)
                    with path.open("w", newline="", encoding="utf-8") as rewrite:
                        writer = csv.DictWriter(rewrite, fieldnames=fieldnames)
                        writer.writeheader()
                        for existing_row in existing_rows:
                            writer.writerow({
                                field: existing_row.get(field, "")
                                for field in fieldnames
                            })
        with path.open("a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            if not file_exists:
                writer.writeheader()
            writer.writerow(row)
