from __future__ import annotations

from dataclasses import dataclass

from core.models import ExecutionEstimate, OrderBook
from core.orderbook import estimate_execution_from_orderbook


@dataclass(frozen=True)
class RoundTripEstimate:
    spot_entry: ExecutionEstimate
    perp_entry: ExecutionEstimate
    spot_exit: ExecutionEstimate
    perp_exit: ExecutionEstimate

    @property
    def round_trip_fillable(self) -> bool:
        return all(
            estimate.is_fillable
            for estimate in (self.spot_entry, self.perp_entry, self.spot_exit, self.perp_exit)
        )


def estimate_basis_round_trip(
    *, direction: str, spot_book: OrderBook, perp_book: OrderBook, notional_usd: float,
) -> RoundTripEstimate:
    if direction == "SHORT_SPOT_LONG_PERP":
        spot_entry_side, perp_entry_side = "sell", "buy"
        spot_exit_side, perp_exit_side = "buy", "sell"
    else:
        spot_entry_side, perp_entry_side = "buy", "sell"
        spot_exit_side, perp_exit_side = "sell", "buy"
    return RoundTripEstimate(
        spot_entry=estimate_execution_from_orderbook(spot_book, spot_entry_side, notional_usd),
        perp_entry=estimate_execution_from_orderbook(perp_book, perp_entry_side, notional_usd),
        spot_exit=estimate_execution_from_orderbook(spot_book, spot_exit_side, notional_usd),
        perp_exit=estimate_execution_from_orderbook(perp_book, perp_exit_side, notional_usd),
    )
