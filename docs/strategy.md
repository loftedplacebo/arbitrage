# Futures-Futures Cross-Exchange Arbitrage Strategy

## Purpose

This project is designed to identify and paper-trade market-neutral futures-futures arbitrage opportunities across crypto exchanges.

The core strategy is to monitor perpetual futures markets across multiple exchanges, identify executable pricing inefficiencies, and enter paired long/short positions where the expected net edge is positive after fees, slippage, funding impact, and execution risk.

The long-term objective is to run many small, high-quality trades with strict risk controls, rather than chasing large directional bets.

---

## Strategy Summary

The strategy monitors approved futures pairs across supported exchanges.

When an executable cross-exchange spread appears, the system calculates the expected net edge using:

- executable bid/ask prices
- taker fees
- expected slippage
- order book depth
- funding rate impact
- time to next funding
- position holding assumptions

If the opportunity persists across multiple scans and exceeds the minimum edge threshold, the system may enter using small market/IOC slices with strict slippage protection.

Multiple entries on the same pair are allowed only as slices of a single managed position, subject to symbol-level and exchange-level exposure limits.

The position exits when net profit exceeds the target buffer, or earlier if risk conditions deteriorate.

---

## Core Principle

Trade frequency is an output, not a target.

The strategy should not aim to place hundreds of trades for its own sake. It should only enter trades where the expected net edge remains clearly positive after all costs and risks.

A smaller number of excellent trades is preferable to a large number of marginal trades.

---

## Trade Direction

For each opportunity:

```text
Long the cheaper futures contract.
Short the more expensive futures contract.
```

Executable prices must be used.

```text
Long entry price = ask price on cheaper exchange.
Short entry price = bid price on richer exchange.
```

The raw executable spread is:

```text
entry_spread_pct = (short_bid - long_ask) / long_ask * 100
```

Mid prices should not be used for entry decisions.

---

## Entry Criteria

A trade may only be entered if all of the following conditions are met:

```text
1. Symbol is approved.
2. Instrument class is crypto.
3. Both exchanges have current ticker data.
4. Both exchanges have current order book data.
5. Spread is executable using bid/ask prices.
6. Net expected edge after fees and slippage exceeds the minimum threshold.
7. Funding impact is neutral or positive, or the spread edge is large enough to justify the trade.
8. Signal persists across multiple scans.
9. Order book depth supports the intended trade size.
10. Current symbol exposure is below the configured limit.
11. Current exchange exposure is below the configured limit.
12. There is no stale data warning.
13. Liquidation distance would remain safe after entry.
14. The bot is not already in emergency or cooldown mode.
```

Recommended initial paper-trading thresholds:

```text
min_net_spread_ex_funding_pct = 0.10
min_net_edge_inc_funding_pct = 0.20
persistence_window_scans = 3
min_persistence_count = 2
max_slice_notional_usd = 100 or 500
min_validated_notional_usd = 100
```

For early live trading, thresholds should be more conservative than paper trading.

---

## Trade Sizing

Initial trade size should be small.

Recommended starting sizes:

```text
max_slice_notional_usd = 100
or
max_slice_notional_usd = 500
```

Actual trade size should be:

```text
trade_size = min(configured_max_slice_size, validated_safe_order_book_size)
```

A trade must not be entered simply because the configured size is small. The order book must still prove that both legs can be filled safely.

---

## Execution Rules

The strategy should not use naked market orders without protection.

The preferred execution method is:

```text
Market or IOC orders with strict slippage caps.
```

Execution sequence:

```text
1. Snapshot both order books.
2. Calculate expected fill price for both legs.
3. Confirm the opportunity still exists after fees and slippage.
4. Submit both legs as close together as possible.
5. Confirm both fills.
6. If one leg fails or partially fills, immediately hedge, retry, or close the exposed leg.
7. Record actual fill prices, fees, slippage, and timestamps.
```

The execution engine must assume that partial fills and one-sided fills can happen.

---

## Market Order Policy

Market orders are acceptable only if they are protected by a maximum tolerated slippage rule.

Where supported by the exchange, use market orders with slippage tolerance.

Where not supported, use aggressive limit or IOC orders.

The bot should never send an unlimited market order where the fill price can move beyond the approved edge threshold.

---

## Position Model

The system should not treat repeated entries on the same symbol as independent trades.

Instead, it should treat them as slices of a single managed position.

Example:

```text
Symbol: IDUSDT
Long exchange: Binance
Short exchange: KuCoin
Max symbol exposure: $5,000
Slice size: $250
Max slices: 20
```

The system may add slices while the opportunity remains valid, but total exposure must remain within configured limits.

Each slice should be recorded separately, but risk should be managed at the aggregated position level.

---

## Position-Level Tracking

Each open position should track:

```text
position_id
symbol
long_exchange
short_exchange
total_notional_usd
slice_count
average_long_entry_price
average_short_entry_price
entry_spread_pct
current_spread_pct
entry_net_edge_pct
current_net_edge_pct
realised_funding_pnl
unrealised_spread_pnl
estimated_close_cost
estimated_net_pnl
current_margin_usage
liquidation_distance_long
liquidation_distance_short
created_at
updated_at
status
```

Each slice should track:

```text
slice_id
position_id
entry_time
notional_usd
long_order_id
short_order_id
long_fill_price
short_fill_price
entry_fees
entry_slippage
entry_reason
```

---

## Exit Criteria

The strategy must not exit only when profitable.

Positions should be closed when profit is achieved, when the trade thesis is invalidated, or when risk conditions deteriorate.

Exit if any of the following are true:

```text
1. Estimated close PnL after fees and slippage exceeds the take-profit target.
2. Spread has compressed by the required percentage.
3. Funding-adjusted edge has disappeared.
4. Funding has flipped materially against the position.
5. Stop-loss threshold has been reached.
6. Liquidation distance has fallen below the safety threshold.
7. Liquidity is no longer sufficient to close safely.
8. Exchange data is stale or unreliable.
9. One exchange API is unstable or unavailable.
10. Maximum holding period has been reached.
11. Manual or automated kill-switch is triggered.
```

Recommended initial paper-trading exit thresholds:

```text
take_profit_pct = 0.15 to 0.25
spread_compression_exit_pct = 50
stop_loss_pct = -0.30 to -0.50
min_remaining_edge_pct = 0.03
max_hold_hours = 24 unless funding remains strongly favourable
```

---

## Stop-Loss and Liquidation Protection

Every leveraged position must have a stop well before liquidation.

However, because this is a two-legged cross-exchange strategy, individual stop orders are not enough.

If one leg closes, the other leg must be closed or hedged immediately.

Minimum live requirements:

```text
1. Bot-level stop-loss.
2. Exchange-native emergency stop where supported.
3. Liquidation-distance monitor.
4. Paired-leg kill-switch.
5. Emergency close-all function.
```

A stop-loss should be managed at the position level, not only at the individual leg level.

---

## Leverage Policy

Leverage should not be used during initial live testing.

Recommended rollout:

```text
Phase 1: Paper trade only.
Phase 2: Live trading with $100-$500 slices and no leverage.
Phase 3: Gradually increase notional size, still without leverage.
Phase 4: Introduce low leverage only after live execution is proven.
Phase 5: Consider higher leverage only after sustained evidence of stable realised PnL.
```

Leverage increases:

```text
slippage impact
fee drag
liquidation risk
failed-leg damage
margin risk
exchange outage risk
```

The strategy must prove real execution quality before leverage is introduced.

---

## Funding Logic

Funding is part of the trade edge but should not be the only reason to enter.

The best trades have both:

```text
positive spread edge
and
neutral or positive funding impact
```

The system should evaluate:

```text
current funding rate
next funding timestamp
time to next funding
expected funding income or cost
funding direction relative to position
funding persistence score
multi-cycle expected funding
```

If already in a profitable trade and future funding remains favourable, the system may continue holding rather than exiting and re-entering, provided risk remains controlled.

This avoids unnecessary fees and execution friction.

---

## Data Quality Requirements

A trade must not be entered if core data is missing, stale, or unreliable.

Required data:

```text
ticker prices
bid/ask prices
order book depth
funding rates
next funding timestamps
exchange fees
symbol metadata
position and margin data
```

The system should log and reject opportunities where:

```text
order book is missing
funding rate is missing
timestamp is stale
bid/ask spread is abnormal
price differs materially from other venues
exchange response is delayed
symbol mapping is uncertain
```

---

## Risk Limits

The system should support configurable limits for:

```text
max_slice_notional_usd
max_symbol_notional_usd
max_exchange_notional_usd
max_total_open_notional_usd
max_open_positions
max_slices_per_symbol
max_daily_loss_usd
max_daily_trades
max_consecutive_losses
max_exchange_error_count
min_liquidation_distance_pct
```

Initial conservative defaults should be used until paper-trading data proves otherwise.

---

## Cooldown Rules

The bot should enter cooldown if:

```text
too many failed orders occur
too many partial fills occur
daily loss limit is reached
exchange API errors exceed threshold
data becomes stale
a kill-switch event occurs
```

During cooldown, the bot should not enter new trades.

It may continue monitoring and may close existing positions if required.

---

## Paper Trading Requirements

Before live trading, the system must paper trade for at least 15 days.

Paper trading must log:

```text
all scanned opportunities
all rejected opportunities
entry decisions
exit decisions
simulated fills
simulated fees
simulated slippage
funding accruals
unrealised PnL
realised PnL
missed opportunities
exit reasons
data quality warnings
```

Paper results should be analysed before live deployment.

The goal of paper trading is to validate:

```text
whether the displayed edge survives fees and slippage
whether funding is paid as expected
whether order book depth is reliable
whether spread convergence happens often enough
whether exit rules behave sensibly
which symbols and exchanges are most reliable
```

---

## Live Trading Rollout

Live deployment should begin with small capital and no leverage.

Recommended live rollout:

```text
1. Start with very small notional per slice.
2. Use no leverage.
3. Trade only the most reliable exchanges and symbols.
4. Confirm real fills match expected fills.
5. Confirm fees match assumptions.
6. Confirm funding payments match recorded rates.
7. Increase size gradually only after realised PnL validates the model.
```

Initial live deployment should be treated as execution validation, not profit maximisation.

---

## Implementation Priorities

Recommended build order:

```text
1. Scanner data collection.
2. Opportunity calculation.
3. Order book depth validation.
4. Paper trading ledger.
5. Entry decision engine.
6. Exit decision engine.
7. Position and slice tracking.
8. Risk limit engine.
9. Funding accrual model.
10. Live execution adapter.
11. Emergency kill-switch.
12. Leverage support only after successful live testing.
```

---

## Non-Negotiable Rules

```text
Do not enter trades from mid prices.
Do not use naked market orders without slippage protection.
Do not treat multiple same-symbol entries as independent risk.
Do not use leverage in initial live testing.
Do not hold losing trades indefinitely waiting for profit.
Do not ignore funding flips.
Do not enter if either leg cannot be closed safely.
Do not continue trading during API instability.
Do not scale until actual fills and realised PnL prove the edge.
```

---

## Strategy Philosophy

This is not a directional trading strategy.

It is an execution-sensitive, market-neutral arbitrage strategy.

The edge is expected to be small, so discipline matters more than prediction.

The system should prioritise:

```text
execution quality
risk control
fee control
slippage control
data quality
capital efficiency
repeatability
```

The correct goal is not to trade more.

The correct goal is to only trade when the expected edge is strong enough to survive real-world execution.
