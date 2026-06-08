from __future__ import annotations

from collections import Counter

from strategy.config import StrategyConfig
from strategy.models import Position, RiskDecision, ValidatedOpportunity


def evaluate_entry_risk(
    opportunity: ValidatedOpportunity,
    open_positions: dict[str, Position],
    config: StrategyConfig,
    desired_notional_usd: float,
    daily_entry_count: int = 0,
    daily_realised_pnl_usd: float = 0.0,
    consecutive_losses: int = 0,
) -> RiskDecision:
    if config.cooldown_enabled:
        return RiskDecision(False, "cooldown_enabled")

    if daily_entry_count >= config.max_daily_entries:
        return RiskDecision(False, "max_daily_entries_reached")

    if daily_realised_pnl_usd <= -abs(config.max_daily_loss_usd):
        return RiskDecision(False, "max_daily_loss_reached")

    if consecutive_losses >= config.max_consecutive_losses:
        return RiskDecision(False, "max_consecutive_losses_reached")

    active_positions = [p for p in open_positions.values() if p.status == "OPEN"]
    if opportunity.position_key not in open_positions and len(active_positions) >= config.max_open_positions:
        return RiskDecision(False, "max_open_positions_reached")

    total_open = sum(p.total_notional_usd for p in active_positions)
    if total_open + desired_notional_usd > config.max_total_open_notional_usd:
        return RiskDecision(False, "max_total_open_notional_reached")

    symbol_open = sum(p.total_notional_usd for p in active_positions if p.symbol == opportunity.symbol)
    if symbol_open + desired_notional_usd > config.max_symbol_notional_usd:
        return RiskDecision(False, "max_symbol_notional_reached")

    exchange_exposure = Counter()
    for position in active_positions:
        exchange_exposure[position.long_exchange] += position.total_notional_usd
        exchange_exposure[position.short_exchange] += position.total_notional_usd

    if exchange_exposure[opportunity.long_exchange] + desired_notional_usd > config.max_exchange_notional_usd:
        return RiskDecision(False, "max_exchange_notional_reached")

    if exchange_exposure[opportunity.short_exchange] + desired_notional_usd > config.max_exchange_notional_usd:
        return RiskDecision(False, "max_exchange_notional_reached")

    existing = open_positions.get(opportunity.position_key)
    if existing and existing.close_liquidity_warning_count > 0:
        return RiskDecision(False, "existing_position_close_liquidity_warning")

    if existing and existing.total_notional_usd > 0:
        if (
            config.block_adds_when_funding_negative
            and opportunity.funding_benefit_pct is not None
            and opportunity.funding_benefit_pct < 0
        ):
            return RiskDecision(False, "existing_position_negative_funding_no_add")

        existing_pnl_pct = (existing.estimated_net_pnl / existing.total_notional_usd) * 100
        if existing_pnl_pct < config.max_existing_position_loss_pct_for_add:
            return RiskDecision(False, "existing_position_too_negative_to_add")

    if existing and existing.slice_count >= config.max_slices_per_position:
        return RiskDecision(False, "max_slices_per_position_reached")

    return RiskDecision(True, "risk_ok")
