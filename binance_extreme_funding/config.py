from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class BinanceExtremeFundingConfig:
    exchange: str = "BINANCE"
    min_abs_funding_rate_pct: float = 0.50
    min_minutes_before_funding: float = 15.0
    min_consistent_observations: int = 2
    min_signal_age_minutes: float = 1.0
    min_layer_interval_minutes: float = 1.0
    layer_ladder_usd: tuple[float, ...] = (100.0, 250.0, 500.0, 1_000.0)
    max_symbol_notional_usd: float = 5_000.0
    max_total_notional_usd: float = 20_000.0
    max_open_positions: int = 40
    basis_take_profit_pct: float = 0.75
    basis_near_flat_exit_abs_pct: float = 0.50
    min_hold_funding_rate_pct: float = 0.30
    juicy_hold_funding_rate_pct: float = 1.00
    min_expected_edge_pct: float = 0.02
    estimated_spot_taker_fee_pct: float = 0.10
    estimated_perp_taker_fee_pct: float = 0.05
    estimated_exit_fee_pct: float = 0.15
    safety_buffer_pct: float = 0.03
    max_entry_exit_cost_pct: float = 1.00
    max_snapshot_age_seconds: float = 180.0
    max_orderbook_age_ms: float = 1_000.0
    basis_history_lookback: int = 15
    min_basis_observations_for_stats: int = 5
    short_spot_entry_max_basis_percentile: float = 25.0
    long_spot_entry_min_basis_percentile: float = 75.0
    max_basis_std_pct: float = 5.0
    max_basis_abs_trend_pct: float = 5.0
    volatility_cooldown_minutes: float = 60.0
    gentle_unwind_chunk_ladder_usd: tuple[float, ...] = (100.0, 250.0, 500.0)
    funding_harvest_unwind_chunk_usd: float = 100.0
    min_funding_harvest_profit_usd: float = 0.25
    full_exit_min_profit_pct: float = 0.02
    post_close_cooldown_minutes: float = 60.0
    fallback_funding_interval_hours: float = 8.0
    scanner_interval_seconds: float = 60.0
    strategy_interval_seconds: float = 60.0
    request_timeout_seconds: float = 20.0
    data_dir: Path = REPO_ROOT / "data" / "binance_extreme_funding"
    snapshots_dir: Path = data_dir / "snapshots"
    opportunities_dir: Path = data_dir / "opportunities"
    paper_dir: Path = data_dir / "paper"

    @property
    def round_trip_fees_pct(self) -> float:
        return (
            self.estimated_spot_taker_fee_pct
            + self.estimated_perp_taker_fee_pct
            + self.estimated_exit_fee_pct
            + self.safety_buffer_pct
        )


DEFAULT_CONFIG = BinanceExtremeFundingConfig()
