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

    if config.require_paper_ready and not opportunity.paper_ready:
        return EntryDecision(False, "paper_ready_false", opportunity.opportunity_key)

    if not opportunity.long_fillable or not opportunity.short_fillable:
        return EntryDecision(False, "not_fillable", opportunity.opportunity_key)

    if opportunity.long_avg_price is None or opportunity.short_avg_price is None:
        return EntryDecision(False, "missing_executable_prices", opportunity.opportunity_key)

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

    normal_timing_ok = normal_entry_funding_timing_ok(opportunity, config, now)
    normal_spread_ok = (
        opportunity.validated_spread_pct is not None
        and opportunity.validated_spread_pct >= config.min_validated_spread_pct
    )
    normal_edge_ok = (
        config.normal_entries_enabled
        and normal_timing_ok
        and normal_spread_ok
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
        funding_capture_ready
        and opportunity.net_edge_ex_funding_pct >= config.funding_capture_min_net_spread_ex_funding_pct
        and opportunity.net_edge_inc_funding_pct >= config.funding_capture_min_net_edge_inc_funding_pct
    )

    if not normal_edge_ok and not funding_capture_edge_ok:
        if funding_capture_ready:
            return EntryDecision(False, "funding_capture_edge_below_threshold", opportunity.opportunity_key)
        if (
            config.funding_capture_enabled
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

    desired_notional = min(config.max_slice_notional_usd, opportunity.notional_usdt)
    risk = evaluate_entry_risk(
        opportunity=opportunity,
        open_positions=open_positions,
        config=config,
        desired_notional_usd=desired_notional,
        daily_entry_count=daily_entry_count,
        daily_realised_pnl_usd=daily_realised_pnl_usd,
        consecutive_losses=consecutive_losses,
    )
    if not risk.allowed:
        return EntryDecision(False, risk.reason, opportunity.opportunity_key, desired_notional)

    reason = "entry_ok" if normal_edge_ok else "funding_capture_entry_ok"
    return EntryDecision(True, reason, opportunity.opportunity_key, desired_notional)
