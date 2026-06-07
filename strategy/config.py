from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class StrategyConfig:
    data_dir: Path = REPO_ROOT / "data" / "strategy"
    validated_input_dir: Path = REPO_ROOT / "data" / "validated_futures_futures_snapshots"

    approved_instrument_class: str = "crypto"
    approved_symbols: set[str] = field(default_factory=set)
    blocked_symbols: set[str] = field(default_factory=lambda: {
        "ANTHROPICUSDT",
        "APLDUSDT",
        "BPUSDT",
        "OKLOUSDT",
        "QNTSTOCKUSDT",
        "RDDTUSDT",
        "SOXSUSDT",
        "SQQQUSDT",
    })

    max_slice_notional_usd: float = 500.0
    min_validated_notional_usd: float = 100.0
    max_symbol_notional_usd: float = 2_500.0
    max_exchange_notional_usd: float = 7_500.0
    max_total_open_notional_usd: float = 10_000.0
    max_open_positions: int = 50
    # TODO: Add max_open_routes_per_symbol to prevent over-concentration across
    # multiple exchange routes for the same underlying symbol.
    max_slices_per_position: int = 3
    max_daily_loss_usd: float = 250.0
    max_daily_entries: int = 500
    max_consecutive_losses: int = 10

    # Normal spread trades should have a high spread and high net edge.
    # These thresholds are intentionally conservative because the entry signal
    # must survive close-side fees, close slippage, and spread movement.
    min_net_spread_ex_funding_pct: float = 0.50
    min_net_edge_inc_funding_pct: float = 0.50
    min_validated_spread_pct: float = 0.75
    normal_entry_min_minutes_to_funding: float = 60.0
    normal_entry_allow_near_funding_if_benefit_pct: float = 0.05
    min_persistence_count: int = 2
    require_paper_ready: bool = True

    # Fixed take-profit for the next paper run. Longer term this should become
    # dynamic based on entry edge.
    take_profit_pct: float = 0.35
    use_dynamic_take_profit: bool = True
    min_take_profit_pct: float = 0.35
    take_profit_edge_fraction: float = 0.35
    max_take_profit_pct: float = 1.00
    spread_compression_exit_pct: float = 50.0
    # Paper experiment: disabled to test whether hedged spreads eventually
    # mean-revert when we stop crystallising spread-widening losses. Keep the
    # threshold available so it can be re-enabled without code changes.
    stop_loss_enabled: bool = False
    stop_loss_pct: float = -1.00
    min_remaining_edge_pct: float = 0.03
    min_profit_to_exit_remaining_edge_pct: float = 0.05
    max_hold_hours: float = 24.0
    max_missing_scans_before_exit: int = 3
    exit_on_missing_opportunity: bool = False
    max_existing_position_loss_pct_for_add: float = -0.30

    estimated_entry_fee_pct: float = 0.10
    estimated_exit_fee_pct: float = 0.10
    estimated_close_slippage_pct: float = 0.02
    max_data_age_seconds: int = 180
    cooldown_enabled: bool = False

    funding_capture_enabled: bool = True
    funding_capture_window_minutes: float = 90.0
    min_minutes_before_funding_entry: float = 2.0
    # Funding-capture trades can use lower spread thresholds than normal spread
    # trades, but they still need a meaningful spread edge. Funding should
    # enhance the trade, not rescue a weak spread.
    min_funding_benefit_for_capture_pct: float = 0.05
    funding_capture_min_net_spread_ex_funding_pct: float = 0.20
    funding_capture_min_net_edge_inc_funding_pct: float = 0.35
    hold_through_favourable_funding: bool = True
    hold_funding_window_minutes: float = 30.0
    funding_exit_decision_window_minutes: float = 15.0
    exit_negative_funding_if_losing: bool = True
    funding_flip_exit_requires_loss: bool = True
    max_negative_funding_tolerated_pct: float = -0.03
    min_profit_to_hold_negative_funding_pct: float = 0.10
    max_projected_negative_funding_cost_pct: float = -0.05
    close_liquidity_exit_requires_hard_risk: bool = True
    close_liquidity_max_warning_scans: int = 3


DEFAULT_CONFIG = StrategyConfig()
