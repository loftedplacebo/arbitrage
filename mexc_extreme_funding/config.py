from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class MexcExtremeFundingConfig:
    exchange: str = "MEXC"
    min_abs_funding_rate_pct: float = 0.50
    min_minutes_before_funding: float = 15.0
    min_consistent_observations: int = 2
    min_signal_age_minutes: float = 1.0
    layer_interval_minutes: float = 30.0
    layer_ladder_usd: tuple[float, ...] = (50.0, 100.0, 250.0, 500.0)
    max_symbol_notional_usd: float = 2_000.0
    max_total_notional_usd: float = 10_000.0
    max_open_positions: int = 30
    basis_take_profit_pct: float = 0.75
    max_adverse_basis_pct: float = 1.00
    max_hold_hours: float = 12.0
    round_trip_fees_pct: float = 0.32
    scanner_interval_seconds: float = 60.0
    strategy_interval_seconds: float = 60.0
    request_timeout_seconds: float = 20.0
    request_sleep_seconds: float = 0.11
    data_dir: Path = REPO_ROOT / "data" / "mexc_extreme_funding"
    snapshots_dir: Path = data_dir / "snapshots"
    paper_dir: Path = data_dir / "paper"


DEFAULT_CONFIG = MexcExtremeFundingConfig()
