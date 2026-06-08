from __future__ import annotations

from datetime import datetime, timezone

from strategy.config import StrategyConfig
from strategy.models import ExitDecision, Position, ValidatedOpportunity


def favourable_funding_event_soon(
    opportunity: ValidatedOpportunity,
    config: StrategyConfig,
    now: datetime,
) -> bool:
    if not config.hold_through_favourable_funding:
        return False

    if opportunity.funding_benefit_pct is None:
        return False

    if opportunity.funding_benefit_pct < config.min_funding_benefit_for_capture_pct:
        return False

    min_minutes = opportunity.min_minutes_to_funding(now)
    if min_minutes is None:
        return False

    return 0 <= min_minutes <= config.hold_funding_window_minutes


def negative_funding_event_near(
    opportunity: ValidatedOpportunity,
    config: StrategyConfig,
    now: datetime,
) -> bool:
    min_minutes = opportunity.min_minutes_to_funding(now)
    if min_minutes is None:
        return False
    return 0 <= min_minutes <= config.funding_exit_decision_window_minutes


def calculate_take_profit_pct(position: Position, config: StrategyConfig) -> float:
    if not config.use_dynamic_take_profit:
        return config.take_profit_pct

    dynamic_take_profit = position.entry_net_edge_pct * config.take_profit_edge_fraction
    return min(
        config.max_take_profit_pct,
        max(config.min_take_profit_pct, dynamic_take_profit),
    )


def estimate_position_pnl(
    position: Position,
    opportunity: ValidatedOpportunity,
    config: StrategyConfig,
) -> tuple[float, float, float]:
    if opportunity.long_close_avg_price is None or opportunity.short_close_avg_price is None:
        return position.estimated_net_pnl, position.unrealised_spread_pnl, position.estimated_close_cost

    long_qty = position.total_notional_usd / position.average_long_entry_price
    short_qty = position.total_notional_usd / position.average_short_entry_price

    long_pnl = long_qty * (opportunity.long_close_avg_price - position.average_long_entry_price)
    short_pnl = short_qty * (position.average_short_entry_price - opportunity.short_close_avg_price)
    unrealised_spread_pnl = long_pnl + short_pnl

    close_slippage_pct = (
        opportunity.close_slippage_pct
        if opportunity.close_slippage_pct is not None
        else config.estimated_close_slippage_pct
    )
    close_cost_pct = config.estimated_exit_fee_pct + close_slippage_pct
    close_cost = position.total_notional_usd * (close_cost_pct / 100)
    estimated_net_pnl = unrealised_spread_pnl + position.realised_funding_pnl - close_cost
    return estimated_net_pnl, unrealised_spread_pnl, close_cost


def evaluate_exit(
    position: Position,
    opportunity: ValidatedOpportunity | None,
    config: StrategyConfig,
    now: datetime | None = None,
) -> ExitDecision:
    now = now or datetime.now(timezone.utc)

    if opportunity is None:
        estimated_net_pnl_pct = (
            (position.estimated_net_pnl / position.total_notional_usd) * 100
            if position.total_notional_usd > 0
            else 0.0
        )
        if position.missing_scan_count >= config.max_missing_scans_before_exit:
            if config.exit_on_missing_opportunity:
                return ExitDecision(
                    True,
                    "opportunity_missing_too_long",
                    position.estimated_net_pnl,
                    estimated_net_pnl_pct,
                )
            return ExitDecision(
                False,
                "opportunity_missing_unpriced_hold",
                position.estimated_net_pnl,
                estimated_net_pnl_pct,
            )
        return ExitDecision(False, "hold", position.estimated_net_pnl, estimated_net_pnl_pct)

    estimated_net_pnl, _, _ = estimate_position_pnl(position, opportunity, config)
    estimated_net_pnl_pct = (estimated_net_pnl / position.total_notional_usd) * 100

    close_liquidity_bad = (
        not opportunity.long_close_fillable
        or not opportunity.short_close_fillable
    )
    if close_liquidity_bad:
        if config.stop_loss_enabled and estimated_net_pnl_pct <= config.stop_loss_pct:
            return ExitDecision(
                True,
                "close_liquidity_warning_stop_loss",
                estimated_net_pnl,
                estimated_net_pnl_pct,
            )

        if position.close_liquidity_warning_count >= config.close_liquidity_max_warning_scans:
            return ExitDecision(
                True,
                "close_liquidity_warning_persistent",
                estimated_net_pnl,
                estimated_net_pnl_pct,
            )

        return ExitDecision(
            False,
            "close_liquidity_warning_hold",
            estimated_net_pnl,
            estimated_net_pnl_pct,
        )

    if config.stop_loss_enabled and estimated_net_pnl_pct <= config.stop_loss_pct:
        return ExitDecision(True, "stop_loss_reached", estimated_net_pnl, estimated_net_pnl_pct)

    take_profit_pct = calculate_take_profit_pct(position, config)
    if estimated_net_pnl_pct >= take_profit_pct:
        return ExitDecision(True, "take_profit_reached", estimated_net_pnl, estimated_net_pnl_pct)

    if opportunity.net_edge_inc_funding_pct is None:
        return ExitDecision(True, "current_edge_missing", estimated_net_pnl, estimated_net_pnl_pct)

    if opportunity.net_edge_inc_funding_pct < config.min_remaining_edge_pct:
        if favourable_funding_event_soon(opportunity, config, now):
            return ExitDecision(
                False,
                "hold_for_favourable_funding",
                estimated_net_pnl,
                estimated_net_pnl_pct,
            )
        if estimated_net_pnl_pct < config.min_profit_to_exit_remaining_edge_pct:
            return ExitDecision(
                False,
                "remaining_edge_low_but_not_profitable",
                estimated_net_pnl,
                estimated_net_pnl_pct,
            )
        return ExitDecision(True, "remaining_edge_too_low", estimated_net_pnl, estimated_net_pnl_pct)

    if opportunity.funding_benefit_pct is not None and opportunity.funding_benefit_pct < 0:
        if not config.exit_on_negative_funding:
            return ExitDecision(
                False,
                "negative_funding_exit_disabled_hold",
                estimated_net_pnl,
                estimated_net_pnl_pct,
            )

        if not negative_funding_event_near(opportunity, config, now):
            return ExitDecision(
                False,
                "funding_negative_but_not_near_event",
                estimated_net_pnl,
                estimated_net_pnl_pct,
            )

        if config.exit_negative_funding_if_losing and estimated_net_pnl_pct <= 0:
            return ExitDecision(
                True,
                "negative_funding_near_event_losing",
                estimated_net_pnl,
                estimated_net_pnl_pct,
            )

        if opportunity.funding_benefit_pct <= config.max_negative_funding_tolerated_pct:
            return ExitDecision(
                True,
                "negative_funding_near_event_material",
                estimated_net_pnl,
                estimated_net_pnl_pct,
            )

        if opportunity.funding_benefit_pct <= config.max_projected_negative_funding_cost_pct:
            return ExitDecision(
                True,
                "projected_negative_funding_too_high",
                estimated_net_pnl,
                estimated_net_pnl_pct,
            )

        if estimated_net_pnl_pct < config.min_profit_to_hold_negative_funding_pct:
            return ExitDecision(
                True,
                "negative_funding_near_event_profit_too_small",
                estimated_net_pnl,
                estimated_net_pnl_pct,
            )

        return ExitDecision(
            False,
            "hold_negative_funding_small_profit_buffer_ok",
            estimated_net_pnl,
            estimated_net_pnl_pct,
        )

    if opportunity.validated_spread_pct is not None and position.entry_spread_pct > 0:
        compression_pct = (
            (position.entry_spread_pct - opportunity.validated_spread_pct)
            / position.entry_spread_pct
        ) * 100
        if compression_pct >= config.spread_compression_exit_pct and estimated_net_pnl > 0:
            return ExitDecision(True, "profitable_spread_compression", estimated_net_pnl, estimated_net_pnl_pct)

    hold_hours = (now - position.created_at).total_seconds() / 3600
    if hold_hours >= config.max_hold_hours:
        if config.max_hold_exit_requires_profit and estimated_net_pnl <= 0:
            return ExitDecision(
                False,
                "max_hold_reached_unprofitable_hold",
                estimated_net_pnl,
                estimated_net_pnl_pct,
            )
        return ExitDecision(True, "max_hold_hours_reached", estimated_net_pnl, estimated_net_pnl_pct)

    return ExitDecision(False, "hold", estimated_net_pnl, estimated_net_pnl_pct)
