from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class MexcExtremeFundingConfig:
    exchange: str = "MEXC"
    min_abs_funding_rate_pct: float = 0.50
    min_minutes_before_funding: float = 7.0
    min_consistent_observations: int = 3
    max_signal_observation_gap_seconds: float = 90.0
    min_layer_interval_minutes: float = 5.0
    layer_ladder_usd: tuple[float, ...] = (50.0, 100.0, 250.0, 500.0)
    layer_min_signal_age_minutes: tuple[float, ...] = (2.0, 15.0, 30.0, 45.0)
    layer_max_minutes_before_funding: tuple[float | None, ...] = (120.0, 60.0, 30.0, 12.0)
    layer_min_conservative_edge_pct: tuple[float, ...] = (0.25, 0.20, 0.15, 0.10)
    funding_prediction_haircuts_pct: tuple[tuple[float, float], ...] = (
        (60.0, 0.25),
        (30.0, 0.20),
        (15.0, 0.12),
        (7.0, 0.05),
    )
    inventory_backed_short_spot_symbols: tuple[str, ...] = ()
    max_residual_delta_pct: float = 0.25
    max_symbol_notional_usd: float = 2_000.0
    max_total_notional_usd: float = 10_000.0
    max_open_positions: int = 30
    basis_take_profit_pct: float = 0.75
    basis_near_flat_exit_abs_pct: float = 0.50
    min_hold_funding_rate_pct: float = 0.30
    juicy_hold_funding_rate_pct: float = 1.00
    min_expected_edge_pct: float = 0.10
    estimated_spot_taker_fee_pct: float = 0.10
    estimated_perp_taker_fee_pct: float = 0.02
    estimated_exit_fee_pct: float = 0.17
    safety_buffer_pct: float = 0.03
    max_entry_exit_cost_pct: float = 0.60
    max_snapshot_age_seconds: float = 180.0
    max_orderbook_age_ms: float = 2_000.0
    basis_history_lookback: int = 15
    min_basis_observations_for_stats: int = 5
    short_spot_entry_max_basis_percentile: float = 25.0
    long_spot_entry_min_basis_percentile: float = 75.0
    max_basis_std_pct: float = 0.75
    max_basis_abs_trend_pct: float = 2.00
    volatility_cooldown_minutes: float = 60.0
    gentle_unwind_chunk_ladder_usd: tuple[float, ...] = (50.0, 100.0, 250.0)
    min_exit_interval_minutes: float = 5.0
    max_chunk_edge_sacrifice_pct: float = 0.05
    funding_harvest_unwind_chunk_usd: float = 50.0
    min_funding_harvest_profit_usd: float = 0.15
    full_exit_min_profit_pct: float = 0.02
    post_close_cooldown_minutes: float = 60.0
    fallback_funding_interval_hours: float = 8.0
    scanner_interval_seconds: float = 60.0
    strategy_interval_seconds: float = 60.0
    request_timeout_seconds: float = 20.0
    request_sleep_seconds: float = 0.11
    data_dir: Path = REPO_ROOT / "data" / "mexc_extreme_funding"
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


DEFAULT_CONFIG = MexcExtremeFundingConfig()
