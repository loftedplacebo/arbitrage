from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Optional


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_bool(value) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes", "y"}


def parse_float(value, default: Optional[float] = None) -> Optional[float]:
    if value in (None, ""):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_int(value, default: int = 0) -> int:
    parsed = parse_float(value)
    if parsed is None:
        return default
    return int(parsed)


def parse_datetime(value) -> Optional[datetime]:
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


def format_datetime(value: Optional[datetime]) -> str:
    return "" if value is None else value.isoformat()


def minutes_until(value: Optional[datetime], now: datetime) -> Optional[float]:
    if value is None:
        return None
    return (value - now).total_seconds() / 60


@dataclass(frozen=True)
class ValidatedOpportunity:
    timestamp_utc: datetime
    symbol: str
    instrument_class: str
    notional_usdt: float
    long_exchange: str
    short_exchange: str
    direction: str
    fast_spread_pct: Optional[float]
    fast_long_ask: Optional[float]
    fast_short_bid: Optional[float]
    long_avg_price: Optional[float]
    short_avg_price: Optional[float]
    long_close_avg_price: Optional[float]
    short_close_avg_price: Optional[float]
    validated_spread_pct: Optional[float]
    long_funding_pct: Optional[float]
    short_funding_pct: Optional[float]
    funding_benefit_pct: Optional[float]
    slippage_pct: Optional[float]
    fees_pct: Optional[float]
    net_edge_ex_funding_pct: Optional[float]
    net_edge_inc_funding_pct: Optional[float]
    classification: str
    long_fillable: bool
    short_fillable: bool
    long_close_fillable: bool
    short_close_fillable: bool
    close_slippage_pct: Optional[float]
    route_observation_count: int
    route_spread_mean_pct: Optional[float]
    route_spread_median_pct: Optional[float]
    route_spread_min_pct: Optional[float]
    route_spread_max_pct: Optional[float]
    route_spread_std_pct: Optional[float]
    route_spread_zscore: Optional[float]
    route_spread_percentile: Optional[float]
    route_spread_trend_pct: Optional[float]
    persistence_count: int
    persistent: bool
    spread_ready: bool
    funding_adjusted_ready: bool
    paper_ready: bool
    combined_volume_usdt: Optional[float]
    long_next_funding_time_utc: Optional[datetime]
    short_next_funding_time_utc: Optional[datetime]

    @property
    def position_key(self) -> str:
        return f"{self.symbol}|{self.long_exchange}|{self.short_exchange}"

    @property
    def opportunity_key(self) -> str:
        return f"{self.position_key}|{int(self.notional_usdt)}"

    def long_minutes_to_funding(self, now: datetime) -> Optional[float]:
        return minutes_until(self.long_next_funding_time_utc, now)

    def short_minutes_to_funding(self, now: datetime) -> Optional[float]:
        return minutes_until(self.short_next_funding_time_utc, now)

    def min_minutes_to_funding(self, now: datetime) -> Optional[float]:
        values = [
            value
            for value in [
                self.long_minutes_to_funding(now),
                self.short_minutes_to_funding(now),
            ]
            if value is not None
        ]
        return min(values) if values else None

    @classmethod
    def from_csv_row(cls, row: dict) -> "ValidatedOpportunity":
        timestamp = parse_datetime(row.get("timestamp_utc"))
        if timestamp is None:
            raise ValueError("validated opportunity row has no valid timestamp_utc")

        return cls(
            timestamp_utc=timestamp,
            symbol=str(row.get("symbol", "")).strip(),
            instrument_class=str(row.get("instrument_class", "")).strip(),
            notional_usdt=parse_float(row.get("notional_usdt"), 0.0) or 0.0,
            long_exchange=str(row.get("long_exchange", "")).strip(),
            short_exchange=str(row.get("short_exchange", "")).strip(),
            direction=str(row.get("direction", "")).strip(),
            fast_spread_pct=parse_float(row.get("fast_spread_pct")),
            fast_long_ask=parse_float(row.get("fast_long_ask")),
            fast_short_bid=parse_float(row.get("fast_short_bid")),
            long_avg_price=parse_float(row.get("long_avg_price")),
            short_avg_price=parse_float(row.get("short_avg_price")),
            long_close_avg_price=parse_float(row.get("long_close_avg_price")),
            short_close_avg_price=parse_float(row.get("short_close_avg_price")),
            validated_spread_pct=parse_float(row.get("validated_spread_pct")),
            long_funding_pct=parse_float(row.get("long_funding_pct")),
            short_funding_pct=parse_float(row.get("short_funding_pct")),
            funding_benefit_pct=parse_float(row.get("funding_benefit_pct")),
            slippage_pct=parse_float(row.get("slippage_pct")),
            fees_pct=parse_float(row.get("fees_pct")),
            net_edge_ex_funding_pct=parse_float(row.get("net_edge_ex_funding_pct")),
            net_edge_inc_funding_pct=parse_float(row.get("net_edge_inc_funding_pct")),
            classification=str(row.get("classification", "")).strip(),
            long_fillable=parse_bool(row.get("long_fillable")),
            short_fillable=parse_bool(row.get("short_fillable")),
            long_close_fillable=parse_bool(row.get("long_close_fillable", row.get("long_fillable"))),
            short_close_fillable=parse_bool(row.get("short_close_fillable", row.get("short_fillable"))),
            close_slippage_pct=parse_float(row.get("close_slippage_pct")),
            route_observation_count=parse_int(row.get("route_observation_count")),
            route_spread_mean_pct=parse_float(row.get("route_spread_mean_pct")),
            route_spread_median_pct=parse_float(row.get("route_spread_median_pct")),
            route_spread_min_pct=parse_float(row.get("route_spread_min_pct")),
            route_spread_max_pct=parse_float(row.get("route_spread_max_pct")),
            route_spread_std_pct=parse_float(row.get("route_spread_std_pct")),
            route_spread_zscore=parse_float(row.get("route_spread_zscore")),
            route_spread_percentile=parse_float(row.get("route_spread_percentile")),
            route_spread_trend_pct=parse_float(row.get("route_spread_trend_pct")),
            persistence_count=parse_int(row.get("persistence_count")),
            persistent=parse_bool(row.get("persistent")),
            spread_ready=parse_bool(row.get("spread_ready")),
            funding_adjusted_ready=parse_bool(row.get("funding_adjusted_ready")),
            paper_ready=parse_bool(row.get("paper_ready")),
            combined_volume_usdt=parse_float(row.get("combined_volume_usdt")),
            long_next_funding_time_utc=parse_datetime(row.get("long_next_funding_time_utc")),
            short_next_funding_time_utc=parse_datetime(row.get("short_next_funding_time_utc")),
        )


@dataclass(frozen=True)
class EntryDecision:
    should_enter: bool
    reason: str
    opportunity_key: str
    desired_notional_usd: float = 0.0


@dataclass(frozen=True)
class ExitDecision:
    should_exit: bool
    reason: str
    estimated_net_pnl_usd: float = 0.0
    estimated_net_pnl_pct: float = 0.0


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reason: str


@dataclass
class Position:
    position_id: str
    symbol: str
    long_exchange: str
    short_exchange: str
    total_notional_usd: float
    slice_count: int
    average_long_entry_price: float
    average_short_entry_price: float
    entry_spread_pct: float
    current_spread_pct: float
    entry_net_edge_pct: float
    current_net_edge_pct: float
    realised_funding_pnl: float
    unrealised_spread_pnl: float
    estimated_close_cost: float
    estimated_net_pnl: float
    created_at: datetime
    updated_at: datetime
    missing_scan_count: int = 0
    close_liquidity_warning_count: int = 0
    status: str = "OPEN"

    def to_csv_row(self) -> dict:
        row = asdict(self)
        row["created_at"] = format_datetime(self.created_at)
        row["updated_at"] = format_datetime(self.updated_at)
        return row

    @classmethod
    def from_csv_row(cls, row: dict) -> "Position":
        created_at = parse_datetime(row.get("created_at")) or utc_now()
        updated_at = parse_datetime(row.get("updated_at")) or created_at
        return cls(
            position_id=str(row.get("position_id", "")),
            symbol=str(row.get("symbol", "")),
            long_exchange=str(row.get("long_exchange", "")),
            short_exchange=str(row.get("short_exchange", "")),
            total_notional_usd=parse_float(row.get("total_notional_usd"), 0.0) or 0.0,
            slice_count=parse_int(row.get("slice_count")),
            average_long_entry_price=parse_float(row.get("average_long_entry_price"), 0.0) or 0.0,
            average_short_entry_price=parse_float(row.get("average_short_entry_price"), 0.0) or 0.0,
            entry_spread_pct=parse_float(row.get("entry_spread_pct"), 0.0) or 0.0,
            current_spread_pct=parse_float(row.get("current_spread_pct"), 0.0) or 0.0,
            entry_net_edge_pct=parse_float(row.get("entry_net_edge_pct"), 0.0) or 0.0,
            current_net_edge_pct=parse_float(row.get("current_net_edge_pct"), 0.0) or 0.0,
            realised_funding_pnl=parse_float(row.get("realised_funding_pnl"), 0.0) or 0.0,
            unrealised_spread_pnl=parse_float(row.get("unrealised_spread_pnl"), 0.0) or 0.0,
            estimated_close_cost=parse_float(row.get("estimated_close_cost"), 0.0) or 0.0,
            estimated_net_pnl=parse_float(row.get("estimated_net_pnl"), 0.0) or 0.0,
            created_at=created_at,
            updated_at=updated_at,
            missing_scan_count=parse_int(row.get("missing_scan_count")),
            close_liquidity_warning_count=parse_int(row.get("close_liquidity_warning_count")),
            status=str(row.get("status", "OPEN")),
        )


@dataclass
class PositionSlice:
    slice_id: str
    position_id: str
    entry_time: datetime
    notional_usd: float
    long_order_id: str
    short_order_id: str
    long_fill_price: float
    short_fill_price: float
    entry_fees: float
    entry_slippage: float
    entry_reason: str
    status: str = "OPEN"

    def to_csv_row(self) -> dict:
        row = asdict(self)
        row["entry_time"] = format_datetime(self.entry_time)
        return row

    @classmethod
    def from_csv_row(cls, row: dict) -> "PositionSlice":
        return cls(
            slice_id=str(row.get("slice_id", "")),
            position_id=str(row.get("position_id", "")),
            entry_time=parse_datetime(row.get("entry_time")) or utc_now(),
            notional_usd=parse_float(row.get("notional_usd"), 0.0) or 0.0,
            long_order_id=str(row.get("long_order_id", "")),
            short_order_id=str(row.get("short_order_id", "")),
            long_fill_price=parse_float(row.get("long_fill_price"), 0.0) or 0.0,
            short_fill_price=parse_float(row.get("short_fill_price"), 0.0) or 0.0,
            entry_fees=parse_float(row.get("entry_fees"), 0.0) or 0.0,
            entry_slippage=parse_float(row.get("entry_slippage"), 0.0) or 0.0,
            entry_reason=str(row.get("entry_reason", "")),
            status=str(row.get("status", "OPEN")),
        )
