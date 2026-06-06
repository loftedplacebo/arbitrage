from __future__ import annotations

from uuid import uuid4

from strategy.config import StrategyConfig
from strategy.exit_rules import estimate_position_pnl
from strategy.models import Position, PositionSlice, ValidatedOpportunity, format_datetime, utc_now
from strategy.position_store import CsvPositionStore


def estimate_funding_pnl_since_last_update(
    position: Position,
    opportunity: ValidatedOpportunity,
    config: StrategyConfig,
) -> float:
    """
    Funding accrual placeholder.

    Future logic should accrue funding only when a funding timestamp is crossed.
    For a long/short perp spread:
        - the long leg receives when funding is negative
        - the short leg receives when funding is positive
        - approximate net benefit is short_funding_pct - long_funding_pct

    This returns 0.0 intentionally, so current paper PnL is spread-only minus
    estimated fees and close slippage. Do not treat realised_funding_pnl as
    modelled yet.
    """
    return 0.0


class PaperExecutionEngine:
    def __init__(self, config: StrategyConfig, store: CsvPositionStore):
        self.config = config
        self.store = store

    def open_or_add_slice(
        self,
        opportunity: ValidatedOpportunity,
        positions: dict[str, Position],
        notional_usd: float,
        reason: str,
    ) -> Position:
        if opportunity.long_avg_price is None or opportunity.short_avg_price is None:
            raise ValueError("Cannot paper execute without executable prices")

        now = opportunity.timestamp_utc
        position_id = opportunity.position_key
        entry_fee = notional_usd * (self.config.estimated_entry_fee_pct / 100)
        entry_slippage = opportunity.slippage_pct or 0.0

        existing = positions.get(position_id)
        if existing is None:
            position = Position(
                position_id=position_id,
                symbol=opportunity.symbol,
                long_exchange=opportunity.long_exchange,
                short_exchange=opportunity.short_exchange,
                total_notional_usd=notional_usd,
                slice_count=1,
                average_long_entry_price=opportunity.long_avg_price,
                average_short_entry_price=opportunity.short_avg_price,
                entry_spread_pct=opportunity.validated_spread_pct or 0.0,
                current_spread_pct=opportunity.validated_spread_pct or 0.0,
                entry_net_edge_pct=opportunity.net_edge_inc_funding_pct or 0.0,
                current_net_edge_pct=opportunity.net_edge_inc_funding_pct or 0.0,
                realised_funding_pnl=0.0,
                unrealised_spread_pnl=0.0,
                estimated_close_cost=0.0,
                estimated_net_pnl=-entry_fee,
                created_at=now,
                updated_at=now,
            )
            positions[position_id] = position
        else:
            new_total = existing.total_notional_usd + notional_usd
            existing.average_long_entry_price = (
                (existing.average_long_entry_price * existing.total_notional_usd)
                + (opportunity.long_avg_price * notional_usd)
            ) / new_total
            existing.average_short_entry_price = (
                (existing.average_short_entry_price * existing.total_notional_usd)
                + (opportunity.short_avg_price * notional_usd)
            ) / new_total
            existing.entry_spread_pct = (
                (existing.entry_spread_pct * existing.total_notional_usd)
                + ((opportunity.validated_spread_pct or 0.0) * notional_usd)
            ) / new_total
            existing.entry_net_edge_pct = (
                (existing.entry_net_edge_pct * existing.total_notional_usd)
                + ((opportunity.net_edge_inc_funding_pct or 0.0) * notional_usd)
            ) / new_total
            existing.total_notional_usd = new_total
            existing.slice_count += 1
            existing.updated_at = now
            existing.estimated_net_pnl -= entry_fee
            position = existing

        position_slice = PositionSlice(
            slice_id=f"slice-{uuid4().hex[:12]}",
            position_id=position_id,
            entry_time=now,
            notional_usd=notional_usd,
            long_order_id=f"paper-long-{uuid4().hex[:12]}",
            short_order_id=f"paper-short-{uuid4().hex[:12]}",
            long_fill_price=opportunity.long_avg_price,
            short_fill_price=opportunity.short_avg_price,
            entry_fees=entry_fee,
            entry_slippage=entry_slippage,
            entry_reason=reason,
        )

        self.store.append_slice(position_slice)
        self.store.append_fill(
            {
                "timestamp_utc": format_datetime(now),
                "event_type": "OPEN_SLICE",
                "position_id": position_id,
                "slice_id": position_slice.slice_id,
                "symbol": opportunity.symbol,
                "long_exchange": opportunity.long_exchange,
                "short_exchange": opportunity.short_exchange,
                "notional_usd": f"{notional_usd:.8f}",
                "long_price": f"{opportunity.long_avg_price:.12g}",
                "short_price": f"{opportunity.short_avg_price:.12g}",
                "fees_usd": f"{entry_fee:.8f}",
                "slippage_pct": f"{entry_slippage:.8f}",
                "realised_pnl_usd": "0.00000000",
                "reason": reason,
            }
        )
        return position

    def refresh_position_marks(
        self,
        position: Position,
        opportunity: ValidatedOpportunity,
    ) -> None:
        estimated_net_pnl, unrealised_spread_pnl, close_cost = estimate_position_pnl(
            position=position,
            opportunity=opportunity,
            config=self.config,
        )
        position.realised_funding_pnl += estimate_funding_pnl_since_last_update(
            position=position,
            opportunity=opportunity,
            config=self.config,
        )
        position.current_spread_pct = opportunity.validated_spread_pct or position.current_spread_pct
        position.current_net_edge_pct = opportunity.net_edge_inc_funding_pct or position.current_net_edge_pct
        position.unrealised_spread_pnl = unrealised_spread_pnl
        position.estimated_close_cost = close_cost
        position.estimated_net_pnl = estimated_net_pnl
        position.updated_at = opportunity.timestamp_utc

    def close_position(
        self,
        position: Position,
        opportunity: ValidatedOpportunity | None,
        reason: str,
    ) -> None:
        now = opportunity.timestamp_utc if opportunity is not None else utc_now()
        if opportunity is not None:
            self.refresh_position_marks(position, opportunity)
            long_price = opportunity.long_close_avg_price or 0.0
            short_price = opportunity.short_close_avg_price or 0.0
        else:
            long_price = 0.0
            short_price = 0.0

        close_fee = position.total_notional_usd * (self.config.estimated_exit_fee_pct / 100)
        realised_pnl = position.estimated_net_pnl
        position.status = "CLOSED"
        position.updated_at = now
        self.store.upsert_position(position)

        self.store.append_fill(
            {
                "timestamp_utc": format_datetime(now),
                "event_type": "CLOSE_POSITION",
                "position_id": position.position_id,
                "slice_id": "",
                "symbol": position.symbol,
                "long_exchange": position.long_exchange,
                "short_exchange": position.short_exchange,
                "notional_usd": f"{position.total_notional_usd:.8f}",
                "long_price": f"{long_price:.12g}",
                "short_price": f"{short_price:.12g}",
                "fees_usd": f"{close_fee:.8f}",
                "slippage_pct": f"{self.config.estimated_close_slippage_pct:.8f}",
                "realised_pnl_usd": f"{realised_pnl:.8f}",
                "reason": reason,
            }
        )
