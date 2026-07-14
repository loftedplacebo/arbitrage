from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Optional


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_datetime(value) -> Optional[datetime]:
    if value in (None, ""):
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        result = datetime.fromisoformat(text)
    except ValueError:
        return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def parse_float(value, default: Optional[float] = None) -> Optional[float]:
    if value in (None, ""):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_int(value, default: int = 0) -> int:
    parsed = parse_float(value)
    return default if parsed is None else int(parsed)


def parse_bool(value) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes", "y"}


def iso(value: Optional[datetime]) -> str:
    return "" if value is None else value.astimezone(timezone.utc).isoformat()


def direction_for_rate(rate_pct: float) -> str:
    return "LONG_SPOT_SHORT_PERP" if rate_pct > 0 else "SHORT_SPOT_LONG_PERP"


def benefit_for_direction(direction: str, rate_pct: float) -> float:
    return rate_pct if direction == "LONG_SPOT_SHORT_PERP" else -rate_pct


@dataclass
class FundingSnapshot:
    observed_at_utc: datetime
    exchange: str
    base: str
    spot_symbol: str
    perp_symbol: str
    current_funding_rate_pct: Optional[float]
    predicted_funding_rate_pct: Optional[float]
    next_funding_time_utc: Optional[datetime]
    minutes_to_funding: Optional[float]
    funding_interval_hours: Optional[float]
    index_price: Optional[float]
    mark_price: Optional[float]
    mark_index_basis_pct: Optional[float]
    spot_bid: Optional[float]
    spot_ask: Optional[float]
    perp_bid: Optional[float]
    perp_ask: Optional[float]
    executable_basis_pct: Optional[float]
    eligible: bool
    reason: str

    @property
    def direction(self) -> str:
        return "" if self.current_funding_rate_pct is None else direction_for_rate(self.current_funding_rate_pct)

    @property
    def event_key(self) -> str:
        return f"{self.perp_symbol}|{iso(self.next_funding_time_utc)}|{self.direction}"

    def to_csv_row(self) -> dict:
        row = asdict(self)
        row["observed_at_utc"] = iso(self.observed_at_utc)
        row["next_funding_time_utc"] = iso(self.next_funding_time_utc)
        row["eligible"] = str(self.eligible)
        row["direction"] = self.direction
        row["event_key"] = self.event_key
        return row

    @classmethod
    def from_csv_row(cls, row: dict) -> "FundingSnapshot":
        return cls(
            observed_at_utc=parse_datetime(row.get("observed_at_utc")) or utc_now(),
            exchange=str(row.get("exchange", "MEXC")),
            base=str(row.get("base", "")),
            spot_symbol=str(row.get("spot_symbol", "")),
            perp_symbol=str(row.get("perp_symbol", "")),
            current_funding_rate_pct=parse_float(row.get("current_funding_rate_pct")),
            predicted_funding_rate_pct=parse_float(row.get("predicted_funding_rate_pct")),
            next_funding_time_utc=parse_datetime(row.get("next_funding_time_utc")),
            minutes_to_funding=parse_float(row.get("minutes_to_funding")),
            funding_interval_hours=parse_float(row.get("funding_interval_hours")),
            index_price=parse_float(row.get("index_price")),
            mark_price=parse_float(row.get("mark_price")),
            mark_index_basis_pct=parse_float(row.get("mark_index_basis_pct")),
            spot_bid=parse_float(row.get("spot_bid")),
            spot_ask=parse_float(row.get("spot_ask")),
            perp_bid=parse_float(row.get("perp_bid")),
            perp_ask=parse_float(row.get("perp_ask")),
            executable_basis_pct=parse_float(row.get("executable_basis_pct")),
            eligible=str(row.get("eligible", "")).lower() == "true",
            reason=str(row.get("reason", "")),
        )


@dataclass(frozen=True)
class OpportunityRow:
    timestamp_utc: datetime
    event_key: str
    base: str
    direction: str
    spot_symbol: str
    perp_symbol: str
    funding_rate_pct: Optional[float]
    predicted_funding_rate_pct: Optional[float]
    funding_time_utc: Optional[datetime]
    funding_interval_hours: Optional[float]
    minutes_to_funding: Optional[float]
    basis_pct: Optional[float]
    notional_usd: float
    spot_entry_avg_price: Optional[float]
    perp_entry_avg_price: Optional[float]
    spot_exit_avg_price: Optional[float]
    perp_exit_avg_price: Optional[float]
    spot_entry_slippage_pct: Optional[float]
    perp_entry_slippage_pct: Optional[float]
    spot_exit_slippage_pct: Optional[float]
    perp_exit_slippage_pct: Optional[float]
    expected_edge_pct: Optional[float]
    round_trip_fillable: bool
    decision: str
    reason: str
    basis_observation_count: int = 0
    basis_mean_pct: Optional[float] = None
    basis_std_pct: Optional[float] = None
    basis_percentile: Optional[float] = None
    basis_trend_pct: Optional[float] = None

    def to_csv_row(self) -> dict:
        row = asdict(self)
        row["timestamp_utc"] = iso(self.timestamp_utc)
        row["funding_time_utc"] = iso(self.funding_time_utc)
        row["round_trip_fillable"] = str(self.round_trip_fillable)
        return row

    @classmethod
    def from_csv_row(cls, row: dict) -> "OpportunityRow":
        return cls(
            timestamp_utc=parse_datetime(row.get("timestamp_utc")) or utc_now(),
            event_key=str(row.get("event_key", "")), base=str(row.get("base", "")),
            direction=str(row.get("direction", "")), spot_symbol=str(row.get("spot_symbol", "")),
            perp_symbol=str(row.get("perp_symbol", "")),
            funding_rate_pct=parse_float(row.get("funding_rate_pct")),
            predicted_funding_rate_pct=parse_float(row.get("predicted_funding_rate_pct")),
            funding_time_utc=parse_datetime(row.get("funding_time_utc")),
            funding_interval_hours=parse_float(row.get("funding_interval_hours")),
            minutes_to_funding=parse_float(row.get("minutes_to_funding")),
            basis_pct=parse_float(row.get("basis_pct")),
            notional_usd=parse_float(row.get("notional_usd"), 0.0) or 0.0,
            spot_entry_avg_price=parse_float(row.get("spot_entry_avg_price")),
            perp_entry_avg_price=parse_float(row.get("perp_entry_avg_price")),
            spot_exit_avg_price=parse_float(row.get("spot_exit_avg_price")),
            perp_exit_avg_price=parse_float(row.get("perp_exit_avg_price")),
            spot_entry_slippage_pct=parse_float(row.get("spot_entry_slippage_pct")),
            perp_entry_slippage_pct=parse_float(row.get("perp_entry_slippage_pct")),
            spot_exit_slippage_pct=parse_float(row.get("spot_exit_slippage_pct")),
            perp_exit_slippage_pct=parse_float(row.get("perp_exit_slippage_pct")),
            expected_edge_pct=parse_float(row.get("expected_edge_pct")),
            round_trip_fillable=parse_bool(row.get("round_trip_fillable")),
            decision=str(row.get("decision", "")), reason=str(row.get("reason", "")),
            basis_observation_count=parse_int(row.get("basis_observation_count")),
            basis_mean_pct=parse_float(row.get("basis_mean_pct")),
            basis_std_pct=parse_float(row.get("basis_std_pct")),
            basis_percentile=parse_float(row.get("basis_percentile")),
            basis_trend_pct=parse_float(row.get("basis_trend_pct")),
        )


@dataclass
class PaperPosition:
    position_id: str
    event_key: str
    base: str
    spot_symbol: str
    perp_symbol: str
    direction: str
    layer_index: int
    notional_usd: float
    entry_at_utc: datetime
    updated_at_utc: datetime
    funding_time_utc: Optional[datetime]
    displayed_rate_at_entry_pct: float
    actual_funding_rate_pct: Optional[float]
    entry_basis_pct: float
    current_basis_pct: float
    basis_pnl_pct: float
    funding_pnl_pct: float
    estimated_net_pnl_pct: float
    realised_pnl_usd: float
    status: str
    exit_at_utc: Optional[datetime]
    exit_reason: str
    spot_qty: float = 0.0
    perp_qty: float = 0.0
    spot_entry_price: float = 0.0
    perp_entry_price: float = 0.0
    entry_fees_usd: float = 0.0
    realised_funding_pnl_usd: float = 0.0
    funding_events_captured: int = 0
    funding_interval_hours: Optional[float] = None
    last_layer_at_utc: Optional[datetime] = None

    def to_csv_row(self) -> dict:
        row = asdict(self)
        for key in ("entry_at_utc", "updated_at_utc", "funding_time_utc", "exit_at_utc", "last_layer_at_utc"):
            row[key] = iso(row[key])
        return row

    @classmethod
    def from_csv_row(cls, row: dict) -> "PaperPosition":
        return cls(
            position_id=str(row.get("position_id", "")),
            event_key=str(row.get("event_key", "")),
            base=str(row.get("base", "")),
            spot_symbol=str(row.get("spot_symbol", "")),
            perp_symbol=str(row.get("perp_symbol", "")),
            direction=str(row.get("direction", "")),
            layer_index=parse_int(row.get("layer_index")),
            notional_usd=parse_float(row.get("notional_usd"), 0.0) or 0.0,
            entry_at_utc=parse_datetime(row.get("entry_at_utc")) or utc_now(),
            updated_at_utc=parse_datetime(row.get("updated_at_utc")) or utc_now(),
            funding_time_utc=parse_datetime(row.get("funding_time_utc")),
            displayed_rate_at_entry_pct=parse_float(row.get("displayed_rate_at_entry_pct"), 0.0) or 0.0,
            actual_funding_rate_pct=parse_float(row.get("actual_funding_rate_pct")),
            entry_basis_pct=parse_float(row.get("entry_basis_pct"), 0.0) or 0.0,
            current_basis_pct=parse_float(row.get("current_basis_pct"), 0.0) or 0.0,
            basis_pnl_pct=parse_float(row.get("basis_pnl_pct"), 0.0) or 0.0,
            funding_pnl_pct=parse_float(row.get("funding_pnl_pct"), 0.0) or 0.0,
            estimated_net_pnl_pct=parse_float(row.get("estimated_net_pnl_pct"), 0.0) or 0.0,
            realised_pnl_usd=parse_float(row.get("realised_pnl_usd"), 0.0) or 0.0,
            status=str(row.get("status", "OPEN")),
            exit_at_utc=parse_datetime(row.get("exit_at_utc")),
            exit_reason=str(row.get("exit_reason", "")),
            spot_qty=parse_float(row.get("spot_qty"), 0.0) or 0.0,
            perp_qty=parse_float(row.get("perp_qty"), 0.0) or 0.0,
            spot_entry_price=parse_float(row.get("spot_entry_price"), 0.0) or 0.0,
            perp_entry_price=parse_float(row.get("perp_entry_price"), 0.0) or 0.0,
            entry_fees_usd=parse_float(row.get("entry_fees_usd"), 0.0) or 0.0,
            realised_funding_pnl_usd=parse_float(row.get("realised_funding_pnl_usd"), 0.0) or 0.0,
            funding_events_captured=parse_int(row.get("funding_events_captured")),
            funding_interval_hours=parse_float(row.get("funding_interval_hours")),
            last_layer_at_utc=parse_datetime(row.get("last_layer_at_utc")),
        )
