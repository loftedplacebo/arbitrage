from __future__ import annotations

import csv
from pathlib import Path

from binance_extreme_funding.config import BinanceExtremeFundingConfig
from binance_extreme_funding.models import PaperPosition


POSITION_FIELDS = [
    "position_id", "event_key", "base", "spot_symbol", "perp_symbol", "direction",
    "layer_index", "notional_usd", "entry_at_utc", "updated_at_utc", "funding_time_utc",
    "displayed_rate_at_entry_pct", "actual_funding_rate_pct", "entry_basis_pct",
    "current_basis_pct", "basis_pnl_pct", "funding_pnl_pct", "estimated_net_pnl_pct",
    "realised_pnl_usd", "status", "exit_at_utc", "exit_reason",
    "spot_qty", "perp_qty", "spot_entry_price", "perp_entry_price",
    "entry_fees_usd", "realised_funding_pnl_usd", "funding_events_captured",
    "funding_interval_hours", "last_layer_at_utc", "management_state", "last_exit_at_utc",
    "exit_started_at_utc",
]
SIGNAL_FIELDS = [
    "event_key", "base", "perp_symbol", "direction", "funding_time_utc", "first_seen_utc",
    "last_seen_utc", "observations", "first_rate_pct", "latest_rate_pct", "min_abs_rate_pct",
    "max_abs_rate_pct", "status", "streak_started_utc", "streak_last_seen_utc",
    "streak_observations", "streak_min_abs_rate_pct",
]
FILL_FIELDS = [
    "timestamp_utc", "event_type", "position_id", "event_key", "perp_symbol", "direction",
    "layer_index", "notional_usd", "basis_pct", "funding_rate_pct", "net_pnl_pct",
    "basis_pnl_usd", "funding_pnl_usd", "realised_pnl_usd", "reason",
]
DECISION_FIELDS = [
    "timestamp_utc", "decision", "event_key", "perp_symbol", "allowed", "reason",
    "layer_index", "notional_usd", "minutes_to_funding", "signal_age_minutes",
    "signal_observations", "conservative_edge_pct", "management_state",
]
FUNDING_FIELDS = [
    "timestamp_utc", "position_id", "event_key", "perp_symbol", "funding_time_utc",
    "displayed_rate_pct", "actual_rate_pct", "funding_benefit_pct", "funding_pnl_usd",
]
COOLDOWN_FIELDS = ["timestamp_utc", "base", "direction", "reason", "expires_at_utc"]


class PaperStore:
    def __init__(self, config: BinanceExtremeFundingConfig) -> None:
        self.root = Path(config.paper_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self.positions_path = self.root / "positions.csv"
        self.signals_path = self.root / "signals.csv"
        self.fills_path = self.root / "fills.csv"
        self.decisions_path = self.root / "decisions.csv"
        self.funding_events_path = self.root / "funding_events.csv"
        self.cooldowns_path = self.root / "cooldowns.csv"

    @staticmethod
    def read_rows(path: Path) -> list[dict]:
        if not path.exists():
            return []
        with path.open("r", newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))

    @staticmethod
    def write_rows(path: Path, fields: list[str], rows: list[dict]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows({field: row.get(field, "") for field in fields} for row in rows)

    @staticmethod
    def append_row(path: Path, fields: list[str], row: dict) -> None:
        exists = path.exists() and path.stat().st_size > 0
        if exists:
            with path.open("r", newline="", encoding="utf-8") as handle:
                current_fields = next(csv.reader(handle), [])
            if current_fields != fields:
                existing_rows = PaperStore.read_rows(path)
                PaperStore.write_rows(path, fields, existing_rows)
        with path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            if not exists:
                writer.writeheader()
            writer.writerow({field: row.get(field, "") for field in fields})

    def load_positions(self) -> list[PaperPosition]:
        return [PaperPosition.from_csv_row(row) for row in self.read_rows(self.positions_path)]

    def write_positions(self, positions: list[PaperPosition]) -> None:
        self.write_rows(self.positions_path, POSITION_FIELDS, [position.to_csv_row() for position in positions])

    def load_signals(self) -> dict[str, dict]:
        return {row.get("event_key", ""): row for row in self.read_rows(self.signals_path)}

    def write_signals(self, signals: dict[str, dict]) -> None:
        self.write_rows(self.signals_path, SIGNAL_FIELDS, list(signals.values()))

    def append_fill(self, row: dict) -> None:
        self.append_row(self.fills_path, FILL_FIELDS, row)

    def append_decision(self, row: dict) -> None:
        self.append_row(self.decisions_path, DECISION_FIELDS, row)

    def append_funding(self, row: dict) -> None:
        self.append_row(self.funding_events_path, FUNDING_FIELDS, row)

    def append_cooldown(self, row: dict) -> None:
        self.append_row(self.cooldowns_path, COOLDOWN_FIELDS, row)

    def load_active_cooldowns(self, now) -> dict[tuple[str, str], dict]:
        from binance_extreme_funding.models import parse_datetime

        active: dict[tuple[str, str], dict] = {}
        for row in self.read_rows(self.cooldowns_path):
            expires = parse_datetime(row.get("expires_at_utc"))
            if expires is not None and expires > now:
                active[(row.get("base", ""), row.get("direction", ""))] = row
        return active
