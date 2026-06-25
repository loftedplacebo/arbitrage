from __future__ import annotations

from datetime import datetime, timezone

from strategy.config import StrategyConfig
from strategy.models import EntryDecision, Position, ValidatedOpportunity
from strategy.risk_rules import evaluate_entry_risk


def evaluate_funding_capture_ready(
    opportunity: ValidatedOpportunity,
    config: StrategyConfig,
    now: datetime,
) -> bool:
    if not config.funding_capture_enabled:
        return False

    if opportunity.funding_benefit_pct is None:
        return False

    if opportunity.funding_benefit_pct < config.min_funding_benefit_for_capture_pct:
        return False

    if opportunity.long_next_funding_time_utc is None or opportunity.short_next_funding_time_utc is None:
        return False

    min_minutes = opportunity.min_minutes_to_funding(now)
    if min_minutes is None:
        return False

    if min_minutes < config.min_minutes_before_funding_entry:
        return False

    if min_minutes > config.funding_capture_window_minutes:
        return False

    if not opportunity.long_fillable or not opportunity.short_fillable:
        return False

    return True


def normal_entry_funding_timing_ok(
    opportunity: ValidatedOpportunity,
    config: StrategyConfig,
    now: datetime,
) -> bool:
    min_minutes = opportunity.min_minutes_to_funding(now)

    if min_minutes is None:
        return True

    if min_minutes >= config.normal_entry_min_minutes_to_funding:
        return True

    return (
        opportunity.funding_benefit_pct is not None
        and opportunity.funding_benefit_pct >= config.normal_entry_allow_near_funding_if_benefit_pct
    )


def route_spread_quality_decision(
    opportunity: ValidatedOpportunity,
    config: StrategyConfig,
) -> tuple[bool, str]:
    if not config.require_route_stats_for_entry:
        return True, "route_spread_quality_ok"

    if opportunity.route_observation_count < config.min_route_observations_for_entry:
        return False, "route_observation_count_below_threshold"

    if opportunity.route_spread_percentile is None or opportunity.route_spread_zscore is None:
        return False, "route_stats_missing"

    if opportunity.route_spread_percentile < config.min_route_spread_percentile:
        return False, "route_spread_percentile_below_threshold"

    if opportunity.route_spread_zscore < config.min_route_spread_zscore:
        return False, "route_spread_zscore_below_threshold"

    if (
        opportunity.route_spread_trend_pct is not None
        and opportunity.route_spread_trend_pct > config.max_route_spread_trend_pct
    ):
        return False, "route_spread_still_widening"

    return True, "route_spread_quality_ok"


def round_trip_entry_liquidity_decision(
    opportunity: ValidatedOpportunity,
    config: StrategyConfig,
) -> tuple[bool, str]:
    """Require a small hedged entry and reverse-side close path before opening."""
    if not config.require_entry_round_trip_fillable:
        return True, "round_trip_entry_liquidity_ok"

    if opportunity.notional_usdt + 1e-8 < config.entry_round_trip_notional_usd:
        return False, "round_trip_notional_not_validated"

    if opportunity.long_close_avg_price is None or opportunity.short_close_avg_price is None:
        return False, "round_trip_close_prices_missing"

    if not opportunity.long_close_fillable or not opportunity.short_close_fillable:
        return False, "round_trip_close_not_fillable"

    return True, "round_trip_entry_liquidity_ok"


def scanner_paper_readiness_decision(
    opportunity: ValidatedOpportunity,
    config: StrategyConfig,
) -> tuple[bool, str]:
    if not config.require_paper_ready:
        return True, "paper_ready_not_required"

    if opportunity.paper_ready:
        return True, "paper_ready_ok"

    if opportunity.instrument_class != config.approved_instrument_class:
        return False, "scanner_ready_instrument_rejected"

    if not opportunity.long_fillable or not opportunity.short_fillable:
        return False, "scanner_ready_entry_not_fillable"

    if not opportunity.persistent:
        return False, "scanner_ready_not_persistent"

    if not opportunity.spread_ready:
        return False, "scanner_ready_spread_not_ready"

    if not opportunity.funding_adjusted_ready:
        return False, "scanner_ready_funding_adjusted_not_ready"

    return False, "scanner_paper_ready_false"


def adaptive_scale_quality_ok(
    opportunity: ValidatedOpportunity,
    existing: Position | None,
    config: StrategyConfig,
) -> bool:
    if not config.adaptive_entry_sizing_enabled:
        return False

    if existing is None or existing.total_notional_usd <= 0:
        return False

    if config.adaptive_scale_requires_existing_profit:
        existing_pnl_pct = (existing.estimated_net_pnl / existing.total_notional_usd) * 100
        if existing_pnl_pct < 0:
            return False

    if (
        opportunity.route_spread_percentile is None
        or opportunity.route_spread_percentile < config.adaptive_scale_min_route_spread_percentile
    ):
        return False

    if (
        opportunity.route_spread_zscore is None
        or opportunity.route_spread_zscore < config.adaptive_scale_min_route_spread_zscore
    ):
        return False

    if (
        opportunity.validated_spread_pct is None
        or opportunity.validated_spread_pct < config.adaptive_scale_min_validated_spread_pct
    ):
        return False

    if (
        opportunity.net_edge_ex_funding_pct is None
        or opportunity.net_edge_ex_funding_pct < config.adaptive_scale_min_net_spread_ex_funding_pct
    ):
        return False

    if (
        opportunity.net_edge_inc_funding_pct is None
        or opportunity.net_edge_inc_funding_pct < config.adaptive_scale_min_net_edge_inc_funding_pct
    ):
        return False

    return True


def entry_notional_candidates(
    opportunity: ValidatedOpportunity,
    open_positions: dict[str, Position],
    config: StrategyConfig,
) -> list[float]:
    base_notional = min(
        config.initial_entry_slice_notional_usd,
        config.max_slice_notional_usd,
        opportunity.notional_usdt,
    )
    existing = open_positions.get(opportunity.position_key)
    if not adaptive_scale_quality_ok(opportunity, existing, config):
        return [base_notional]

    max_validated = min(config.max_slice_notional_usd, opportunity.notional_usdt)
    ladder = sorted(
        {
            tier
            for tier in config.entry_slice_ladder_usd
            if config.min_validated_notional_usd <= tier <= max_validated
        },
        reverse=True,
    )
    if base_notional not in ladder and base_notional > 0:
        ladder.append(base_notional)

    return sorted(set(ladder), reverse=True) or [base_notional]


def evaluate_entry(
    opportunity: ValidatedOpportunity,
    open_positions: dict[str, Position],
    config: StrategyConfig,
    now: datetime | None = None,
    daily_entry_count: int = 0,
    daily_realised_pnl_usd: float = 0.0,
    consecutive_losses: int = 0,
) -> EntryDecision:
    now = now or datetime.now(timezone.utc)

    if opportunity.symbol in config.blocked_symbols:
        return EntryDecision(False, "symbol_blocked", opportunity.opportunity_key)

    if config.approved_symbols and opportunity.symbol not in config.approved_symbols:
        return EntryDecision(False, "symbol_not_approved", opportunity.opportunity_key)

    if opportunity.instrument_class != config.approved_instrument_class:
        return EntryDecision(False, "instrument_class_rejected", opportunity.opportunity_key)

    if not opportunity.long_fillable or not opportunity.short_fillable:
        return EntryDecision(False, "not_fillable", opportunity.opportunity_key)

    if opportunity.long_avg_price is None or opportunity.short_avg_price is None:
        return EntryDecision(False, "missing_executable_prices", opportunity.opportunity_key)

    round_trip_ok, round_trip_reason = round_trip_entry_liquidity_decision(opportunity, config)
    if not round_trip_ok:
        return EntryDecision(False, round_trip_reason, opportunity.opportunity_key)

    paper_ready_ok, paper_ready_reason = scanner_paper_readiness_decision(opportunity, config)
    if not paper_ready_ok:
        return EntryDecision(False, paper_ready_reason, opportunity.opportunity_key)

    if opportunity.validated_spread_pct is None:
        return EntryDecision(False, "missing_validated_spread", opportunity.opportunity_key)

    if opportunity.net_edge_ex_funding_pct is None or opportunity.net_edge_inc_funding_pct is None:
        return EntryDecision(False, "missing_net_edge", opportunity.opportunity_key)

    if opportunity.notional_usdt < config.min_validated_notional_usd:
        return EntryDecision(False, "notional_below_min_validated", opportunity.opportunity_key)

    if opportunity.notional_usdt > config.max_slice_notional_usd:
        return EntryDecision(False, "notional_above_max_slice", opportunity.opportunity_key)

    if opportunity.persistence_count < config.min_persistence_count:
        return EntryDecision(False, "persistence_below_threshold", opportunity.opportunity_key)

    age_seconds = (now - opportunity.timestamp_utc).total_seconds()
    if age_seconds > config.max_data_age_seconds:
        return EntryDecision(False, "stale_opportunity", opportunity.opportunity_key)

    route_quality_ok, route_quality_reason = route_spread_quality_decision(opportunity, config)
    normal_timing_ok = normal_entry_funding_timing_ok(opportunity, config, now)
    normal_spread_ok = (
        opportunity.validated_spread_pct is not None
        and opportunity.validated_spread_pct >= config.min_validated_spread_pct
    )
    funding_not_hostile = (
        opportunity.funding_benefit_pct is None
        or opportunity.funding_benefit_pct >= config.max_adverse_funding_for_spread_entry_pct
    )
    normal_edge_ok = (
        config.normal_entries_enabled
        and route_quality_ok
        and normal_timing_ok
        and normal_spread_ok
        and funding_not_hostile
        and opportunity.net_edge_ex_funding_pct >= config.min_net_spread_ex_funding_pct
        and opportunity.net_edge_inc_funding_pct >= config.min_net_edge_inc_funding_pct
    )
    normal_edge_without_timing_ok = (
        normal_spread_ok
        and opportunity.net_edge_ex_funding_pct >= config.min_net_spread_ex_funding_pct
        and opportunity.net_edge_inc_funding_pct >= config.min_net_edge_inc_funding_pct
    )
    funding_capture_ready = evaluate_funding_capture_ready(opportunity, config, now)
    funding_capture_edge_ok = (
        config.funding_capture_entries_enabled
        and route_quality_ok
        and funding_capture_ready
        and opportunity.net_edge_ex_funding_pct >= config.funding_capture_min_net_spread_ex_funding_pct
        and opportunity.net_edge_inc_funding_pct >= config.funding_capture_min_net_edge_inc_funding_pct
    )

    if not normal_edge_ok and not funding_capture_edge_ok:
        if not route_quality_ok:
            return EntryDecision(False, route_quality_reason, opportunity.opportunity_key)
        if not funding_not_hostile:
            return EntryDecision(False, "funding_hostile_for_spread_entry", opportunity.opportunity_key)
        if funding_capture_ready and config.funding_capture_entries_enabled:
            return EntryDecision(False, "funding_capture_edge_below_threshold", opportunity.opportunity_key)
        if (
            config.funding_capture_entries_enabled
            and config.funding_capture_enabled
            and opportunity.funding_benefit_pct is not None
            and opportunity.funding_benefit_pct >= config.min_funding_benefit_for_capture_pct
        ):
            return EntryDecision(False, "funding_capture_not_ready", opportunity.opportunity_key)
        if normal_edge_without_timing_ok and not config.normal_entries_enabled:
            return EntryDecision(False, "normal_entries_disabled", opportunity.opportunity_key)
        if normal_edge_without_timing_ok and not normal_timing_ok:
            return EntryDecision(False, "normal_entry_too_close_to_funding", opportunity.opportunity_key)
        if not normal_spread_ok:
            return EntryDecision(False, "validated_spread_below_threshold", opportunity.opportunity_key)
        if opportunity.net_edge_ex_funding_pct < config.min_net_spread_ex_funding_pct:
            return EntryDecision(False, "net_spread_ex_funding_below_threshold", opportunity.opportunity_key)
        if opportunity.net_edge_inc_funding_pct < config.min_net_edge_inc_funding_pct:
            return EntryDecision(False, "net_edge_inc_funding_below_threshold", opportunity.opportunity_key)
        return EntryDecision(False, "edge_thresholds_not_met", opportunity.opportunity_key)

    reason = "spread_entry_funding_bonus_ok" if normal_edge_ok and funding_capture_ready else "spread_entry_ok"
    if funding_capture_edge_ok:
        reason = "funding_capture_entry_ok"

    candidates = entry_notional_candidates(opportunity, open_positions, config)
    last_risk_reason = "risk_blocked"
    for desired_notional in candidates:
        risk = evaluate_entry_risk(
            opportunity=opportunity,
            open_positions=open_positions,
            config=config,
            desired_notional_usd=desired_notional,
            daily_entry_count=daily_entry_count,
            daily_realised_pnl_usd=daily_realised_pnl_usd,
            consecutive_losses=consecutive_losses,
        )
        if risk.allowed:
            return EntryDecision(True, reason, opportunity.opportunity_key, desired_notional)
        last_risk_reason = risk.reason

    return EntryDecision(False, last_risk_reason, opportunity.opportunity_key, candidates[0])
