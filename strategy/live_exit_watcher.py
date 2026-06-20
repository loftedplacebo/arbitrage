from __future__ import annotations

from datetime import datetime, timezone

from core.models import OrderBook, OrderBookLevel
from core.orderbook import estimate_execution_from_orderbook
from core.scoring import calculate_futures_futures_spread_pct
from strategy.config import StrategyConfig
from strategy.exit_rules import estimate_close_pnl_for_notional
from strategy.models import Position, ValidatedOpportunity, parse_datetime
from strategy.paper_execution import PaperExecutionEngine
from strategy.position_store import CsvPositionStore


class LiveOrderBookCache:
    """Small strategy-local view of scanner-published position order books."""

    def __init__(self) -> None:
        self._books: dict[tuple[str, str], OrderBook] = {}

    def update_payload(self, payload: dict) -> OrderBook | None:
        observed_at = parse_datetime(payload.get("observed_at_utc"))
        exchange = str(payload.get("exchange", "")).strip()
        symbol = str(payload.get("symbol", "")).strip()
        if observed_at is None or not exchange or not symbol:
            return None
        try:
            book = OrderBook(
                exchange=exchange,
                market_type=str(payload.get("market_type", "futures")),
                standard_symbol=symbol,
                exchange_symbol=str(payload.get("exchange_symbol", symbol)),
                bids=[OrderBookLevel(price=float(price), quantity=float(quantity)) for price, quantity in payload.get("bids", [])],
                asks=[OrderBookLevel(price=float(price), quantity=float(quantity)) for price, quantity in payload.get("asks", [])],
                observed_at_utc=observed_at,
            )
        except (TypeError, ValueError):
            return None
        if not book.bids or not book.asks:
            return None
        self._books[(exchange, symbol)] = book
        return book

    def get(self, exchange: str, symbol: str) -> OrderBook | None:
        return self._books.get((exchange, symbol))


def build_live_exit_rows(
    position: Position,
    cache: LiveOrderBookCache,
    config: StrategyConfig,
) -> list[ValidatedOpportunity]:
    long_book = cache.get(position.long_exchange, position.symbol)
    short_book = cache.get(position.short_exchange, position.symbol)
    if long_book is None or short_book is None:
        return []

    timestamp = max(long_book.observed_at_utc, short_book.observed_at_utc)
    chunks = {min(position.total_notional_usd, max(config.partial_exit_chunk_ladder_usd))}
    chunks.update(tier for tier in config.partial_exit_chunk_ladder_usd if tier <= position.total_notional_usd)
    rows = []
    for notional in sorted(chunks):
        long_buy = estimate_execution_from_orderbook(long_book, "buy", notional)
        short_sell = estimate_execution_from_orderbook(short_book, "sell", notional)
        long_close = estimate_execution_from_orderbook(long_book, "sell", notional)
        short_close = estimate_execution_from_orderbook(short_book, "buy", notional)
        spread = (
            calculate_futures_futures_spread_pct(long_buy.average_price, short_sell.average_price)
            if long_buy.is_fillable and short_sell.is_fillable
            else None
        )
        rows.append(
            ValidatedOpportunity(
                timestamp_utc=timestamp,
                symbol=position.symbol,
                instrument_class="crypto",
                notional_usdt=notional,
                long_exchange=position.long_exchange,
                short_exchange=position.short_exchange,
                direction=f"long_{position.long_exchange}_short_{position.short_exchange}",
                fast_spread_pct=spread,
                fast_long_ask=long_buy.average_price,
                fast_short_bid=short_sell.average_price,
                long_avg_price=long_buy.average_price,
                short_avg_price=short_sell.average_price,
                long_close_avg_price=long_close.average_price,
                short_close_avg_price=short_close.average_price,
                validated_spread_pct=spread,
                long_funding_pct=None,
                short_funding_pct=None,
                funding_benefit_pct=None,
                slippage_pct=long_buy.slippage_pct + short_sell.slippage_pct,
                fees_pct=None,
                net_edge_ex_funding_pct=None,
                net_edge_inc_funding_pct=None,
                classification="LIVE_EXIT",
                long_fillable=long_buy.is_fillable,
                short_fillable=short_sell.is_fillable,
                long_close_fillable=long_close.is_fillable,
                short_close_fillable=short_close.is_fillable,
                close_slippage_pct=long_close.slippage_pct + short_close.slippage_pct,
                route_observation_count=0,
                route_spread_mean_pct=None,
                route_spread_median_pct=None,
                route_spread_min_pct=None,
                route_spread_max_pct=None,
                route_spread_std_pct=None,
                route_spread_zscore=None,
                route_spread_percentile=None,
                route_spread_trend_pct=None,
                persistence_count=0,
                persistent=False,
                spread_ready=False,
                funding_adjusted_ready=False,
                paper_ready=False,
                combined_volume_usdt=None,
                long_next_funding_time_utc=None,
                short_next_funding_time_utc=None,
            )
        )
    return rows


def process_live_exit_updates(
    *,
    positions: dict[str, Position],
    cache: LiveOrderBookCache,
    store: CsvPositionStore,
    engine: PaperExecutionEngine,
    config: StrategyConfig,
    changed_exchange: str,
    changed_symbol: str,
) -> int:
    """Unwind only already exit-only positions on a relevant live book update."""
    executed = 0
    for position_id, position in list(positions.items()):
        if not position.exit_only or position.symbol != changed_symbol:
            continue
        if changed_exchange not in {position.long_exchange, position.short_exchange}:
            continue

        rows = build_live_exit_rows(position, cache, config)
        for row in sorted(rows, key=lambda item: item.notional_usdt, reverse=True):
            if not (row.long_close_fillable and row.short_close_fillable):
                continue
            chunk_spread_pnl, close_cost = estimate_close_pnl_for_notional(
                position=position,
                opportunity=row,
                config=config,
                notional_usd=row.notional_usdt,
            )
            chunk_pnl = chunk_spread_pnl - close_cost
            if (chunk_pnl / row.notional_usdt) * 100 < config.partial_exit_min_profit_pct:
                continue
            position_closed, realised_pnl = engine.close_position_chunk(
                position=position,
                opportunity=row,
                notional_usd=row.notional_usdt,
                reason="live_partial_exit",
            )
            store.append_decision(
                decision_type="EXIT",
                symbol=position.symbol,
                position_id=position.position_id,
                opportunity_key=row.opportunity_key,
                allowed=True,
                reason="live_partial_exit_completed" if position_closed else "live_partial_exit_executed",
                notional_usd=row.notional_usdt,
                estimated_net_pnl_usd=position.estimated_net_pnl,
                estimated_net_pnl_pct=(position.estimated_net_pnl / max(position.total_notional_usd, 1.0)) * 100,
                partial_exit_notional_usd=row.notional_usdt,
                partial_exit_pnl_usd=realised_pnl,
                position_exit_only=True,
                entry_net_edge_pct=position.entry_net_edge_pct,
            )
            if position_closed:
                positions.pop(position_id, None)
            executed += 1
            break

    if executed:
        store.write_positions(positions)
    return executed
