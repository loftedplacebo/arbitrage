from __future__ import annotations

import math
from dataclasses import dataclass

from core.models import ExecutionEstimate, OrderBook
from mexc_extreme_funding.models import MexcMarketRules


@dataclass(frozen=True)
class RoundTripEstimate:
    spot_entry: ExecutionEstimate
    perp_entry: ExecutionEstimate
    spot_exit: ExecutionEstimate
    perp_exit: ExecutionEstimate
    rules_valid: bool
    perp_contracts: float
    hedged_base_quantity: float
    residual_delta_pct: float

    @property
    def round_trip_fillable(self) -> bool:
        return self.rules_valid and all(
            estimate.is_fillable
            for estimate in (self.spot_entry, self.perp_entry, self.spot_exit, self.perp_exit)
        )


def _empty_estimate(orderbook: OrderBook, side: str, notional_usd: float) -> ExecutionEstimate:
    return ExecutionEstimate(
        exchange=orderbook.exchange,
        market_type=orderbook.market_type,
        standard_symbol=orderbook.standard_symbol,
        side=side,
        notional_usdt=notional_usd,
        best_price=0.0,
        average_price=0.0,
        filled_quantity=0.0,
        filled_notional=0.0,
        slippage_pct=0.0,
        is_fillable=False,
    )


def _estimate_quantity(orderbook: OrderBook, side: str, quantity: float) -> ExecutionEstimate:
    levels = orderbook.asks if side == "buy" else orderbook.bids
    if quantity <= 0 or not levels:
        return _empty_estimate(orderbook, side, 0.0)
    best_price = levels[0].price
    remaining = quantity
    filled_quantity = 0.0
    filled_notional = 0.0
    for level in levels:
        take_quantity = min(remaining, level.quantity)
        filled_quantity += take_quantity
        filled_notional += take_quantity * level.price
        remaining -= take_quantity
        if remaining <= 1e-12:
            break
    average_price = filled_notional / filled_quantity if filled_quantity > 0 else 0.0
    if average_price <= 0:
        slippage_pct = 0.0
    elif side == "buy":
        slippage_pct = (average_price / best_price - 1) * 100
    else:
        slippage_pct = (best_price / average_price - 1) * 100
    return ExecutionEstimate(
        exchange=orderbook.exchange,
        market_type=orderbook.market_type,
        standard_symbol=orderbook.standard_symbol,
        side=side,
        notional_usdt=filled_notional,
        best_price=best_price,
        average_price=average_price,
        filled_quantity=filled_quantity,
        filled_notional=filled_notional,
        slippage_pct=slippage_pct,
        is_fillable=remaining <= 1e-12,
    )


def _quantity_for_notional(
    *,
    notional_usd: float,
    reference_price: float,
    rules: MexcMarketRules,
) -> tuple[float, float]:
    if (
        notional_usd <= 0
        or reference_price <= 0
        or rules.contract_size <= 0
        or rules.contract_volume_step <= 0
    ):
        return 0.0, 0.0
    target_contracts = notional_usd / reference_price / rules.contract_size
    if (
        target_contracts < rules.min_contract_volume - 1e-12
        or target_contracts > rules.max_contract_volume + 1e-12
    ):
        return 0.0, 0.0
    contracts = math.floor(target_contracts / rules.contract_volume_step + 1e-12) * rules.contract_volume_step
    for _ in range(10_000):
        if contracts < rules.min_contract_volume - 1e-12:
            return 0.0, 0.0
        quantity = contracts * rules.contract_size
        if rules.spot_quantity_step <= 0:
            return contracts, quantity
        spot_units = quantity / rules.spot_quantity_step
        if abs(spot_units - round(spot_units)) <= 1e-8:
            return contracts, quantity
        contracts -= rules.contract_volume_step
    return 0.0, 0.0


def estimate_basis_round_trip(
    *,
    direction: str,
    spot_book: OrderBook,
    perp_book: OrderBook,
    notional_usd: float,
    rules: MexcMarketRules,
) -> RoundTripEstimate:
    if direction == "SHORT_SPOT_LONG_PERP":
        spot_entry_side, perp_entry_side = "sell", "buy"
        spot_exit_side, perp_exit_side = "buy", "sell"
    else:
        spot_entry_side, perp_entry_side = "buy", "sell"
        spot_exit_side, perp_exit_side = "sell", "buy"
    perp_levels = perp_book.asks if perp_entry_side == "buy" else perp_book.bids
    reference_price = perp_levels[0].price if perp_levels else 0.0
    contracts, quantity = _quantity_for_notional(
        notional_usd=notional_usd,
        reference_price=reference_price,
        rules=rules,
    )
    rules_valid = (
        rules.spot_trading_allowed
        and rules.perp_api_allowed
        and rules.perp_state == 0
        and contracts > 0
    )
    if not rules_valid:
        return RoundTripEstimate(
            spot_entry=_empty_estimate(spot_book, spot_entry_side, notional_usd),
            perp_entry=_empty_estimate(perp_book, perp_entry_side, notional_usd),
            spot_exit=_empty_estimate(spot_book, spot_exit_side, notional_usd),
            perp_exit=_empty_estimate(perp_book, perp_exit_side, notional_usd),
            rules_valid=False,
            perp_contracts=contracts,
            hedged_base_quantity=quantity,
            residual_delta_pct=0.0,
        )
    return RoundTripEstimate(
        spot_entry=_estimate_quantity(spot_book, spot_entry_side, quantity),
        perp_entry=_estimate_quantity(perp_book, perp_entry_side, quantity),
        spot_exit=_estimate_quantity(spot_book, spot_exit_side, quantity),
        perp_exit=_estimate_quantity(perp_book, perp_exit_side, quantity),
        rules_valid=True,
        perp_contracts=contracts,
        hedged_base_quantity=quantity,
        residual_delta_pct=0.0,
    )
