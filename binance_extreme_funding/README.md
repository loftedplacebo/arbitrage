# Binance Extreme-Funding Paper Strategy

This package is an independent public-data scanner, paper ledger, strategy, and
dashboard for Binance USDT perpetual funding events. It does not read
`data/funding_lock_research/`, share position state with another exchange, use
API keys, or submit orders.

The strategy targets unusually large displayed funding rates, enters the
funding-receiving spot/perpetual hedge in controlled layers, and manages basis
risk without an adverse-basis stop or a time-based forced exit.

## Activation

The application has three independently supervised processes:

| Process | Command | VPS service | Interval/port |
| --- | --- | --- | --- |
| Scanner | `python -m binance_extreme_funding.run_scanner --loop` | `binance-extreme-funding-scanner` | 60 seconds |
| Paper strategy | `python -m binance_extreme_funding.run_paper_strategy --loop` | `binance-extreme-funding-strategy` | 60 seconds |
| Dashboard | `python -m binance_extreme_funding.run_dashboard --port 8770` | `binance-extreme-funding-dashboard` | `127.0.0.1:8770` |

Starting the service does not activate a trade. A trade becomes active only
after every scanner, signal, execution, and portfolio gate below passes.

## Direction And Basis

| Displayed funding | Paper hedge | Funding benefit |
| --- | --- | --- |
| Positive | Long spot, short perpetual | The short perpetual receives funding |
| Negative | Short spot, long perpetual | The long perpetual receives funding |

Basis is expressed as:

```text
basis_pct = (perpetual_price / spot_price - 1) * 100
```

For `LONG_SPOT_SHORT_PERP`, basis improves when it falls. For
`SHORT_SPOT_LONG_PERP`, basis improves when it rises. Entry and exit calculations
use executable average prices from the relevant side of each order book, not
mark price or midpoint alone.

## Scanner And Source Of Truth

Every scanner pass:

1. Downloads the Binance USDT perpetual premium-index feed, futures top of book,
   spot top of book, and adjusted funding-interval metadata.
2. Records the current/displayed funding rate, next funding time, mark/index
   basis, matching spot market, and top-of-book executable basis.
3. For an extreme candidate or an already open position, downloads 100 levels
   of spot and futures depth.
4. Rejects depth when either book is over one second old or the two REST-book
   observations are more than one second apart.
5. Prices all four round-trip legs for each relevant notional: spot entry,
   perpetual entry, spot exit, and perpetual exit.
6. Writes notional-specific opportunity rows and rolling basis statistics.
7. Writes displayed rates at or above the extreme threshold to a compact daily
   extreme-observation journal.
8. After settlement, reconciles the compact observations with actual Binance
   funding history. The scanner does not reread the large full-market snapshot
   files during every cycle.

`latest_opportunities.csv` is the paper strategy's execution source of truth.
`latest_snapshots.csv` is the latest funding audit feed. Research CSVs are never
used to make live paper decisions.

## Entry Rules

An initial entry or additional layer must pass these rules in order.

### 1. Funding snapshot eligibility

- Absolute displayed funding must be at least `0.50%`.
- At least 15 minutes must remain before the next funding timestamp.
- A matching Binance spot market and valid spot/perpetual prices must exist.
- Direction is derived from the current funding sign.

### 2. Execution opportunity eligibility

- The complete four-leg round trip must be fillable at the tested notional.
- Expected edge must be at least `0.02%` after all modeled entry/exit slippage,
  fees, and safety buffer.
- Estimated exit slippage plus modeled exit fees must not exceed `1.00%`.
- The opportunity row must be no more than 180 seconds old and must belong to
  the newest scanner timestamp.
- The spot and perpetual depth snapshots must each be no more than one second
  old and no more than one second apart.

Expected entry edge is:

```text
funding benefit
- spot entry slippage
- perpetual entry slippage
- spot exit slippage
- perpetual exit slippage
- 0.10% spot entry fee
- 0.05% perpetual entry fee
- 0.15% combined exit fee allowance
- 0.03% safety buffer
```

The fixed fee/safety component is `0.33%` before measured slippage.

### 3. Basis-history gate

The scanner keeps the latest 15 basis observations. Once at least five exist:

- `LONG_SPOT_SHORT_PERP` requires basis at or above the 75th percentile.
- `SHORT_SPOT_LONG_PERP` requires basis at or below the 25th percentile.
- Basis standard deviation above `0.75%`, or an absolute lookback trend above
  `2.00%`, starts a 60-minute entry cooldown.

This directional percentile rule means an orderly adverse widening can improve
the next layer's entry. Disorderly volatility pauses new risk but never forces
the existing hedge to close.

### 4. Signal activation

- The same event key, symbol, funding timestamp, and direction must remain above
  `0.50%` for at least two genuinely consecutive fresh observations.
- The initial probe requires at least two minutes of continuous qualification.
- A below-threshold observation, direction change, stale event, or observation
  gap over 180 seconds resets the streak to zero. Lifetime observations remain
  available for research but cannot reactivate a broken streak.
- Repeated strategy runs over the same scanner timestamp do not create another
  observation.

### 5. Layer and portfolio gate

- Entry ladder: `$100`, `$250`, `$500`, `$1,000`.
- The first position uses `$100`; each later add uses the next ladder value.
- The `$100` probe requires a two-minute qualifying streak and may enter at any
  eligible point at least 15 minutes before funding.
- The `$250` layer requires a ten-minute streak and at most 120 minutes to
  funding.
- The `$500` layer requires a 30-minute streak, at most 60 minutes to funding,
  and at least `0.10%` conservative edge.
- The `$1,000` layer requires a 60-minute streak, at most 30 minutes to funding,
  and at least `0.10%` conservative edge.
- Layers above the probe use the minimum absolute rate in the current streak and
  subtract a lead-time prediction haircut: `0.40%` beyond 120 minutes, `0.20%`
  from 60-120, `0.10%` from 30-60, and `0.06%` from 15-30 minutes.
- The `$250` layer must retain at least `0.02%` conservative edge after its
  haircut. The probe uses current depth-priced edge without a prediction
  haircut.
- At most one layer for a base/direction is added per strategy cycle.
- At least one minute must pass since the previous layer.
- Layers are aggregated into one position with weighted spot price, perpetual
  price, basis, displayed funding, quantities, and entry fees.
- Maximum open notional per symbol is `$5,000`.
- Maximum total strategy notional is `$20,000`.
- Maximum aggregated open positions is 40.
- An opposite-direction position in the same base blocks entry.
- A full close starts a 60-minute base/direction re-entry cooldown.

The four-step ladder is finite for a position. Unlike KuCoin, Binance does not
continue adding the same chunk indefinitely after all four layers are used.

## Hold Rules

There is no maximum holding time and no adverse-basis liquidation rule.

Before the first funding event:

- Hold when basis is flat or adverse.
- Continue considering the next controlled layer if all entry gates still pass.
- Stale, missing, volatile, or unfillable data pauses new entries and exits; it
  does not close the position.
- The only pre-funding exit path is the profitable basis rule below.

After at least one funding event:

1. If the next funding benefit for the held direction is at least `1.00%`, hold
   for the next funding event even if basis has improved.
2. If next funding is from `0.30%` up to but excluding `1.00%`, request a
   profitable unwind.
3. If next funding is missing, below `0.30%`, or has reversed against the held
   direction, request a weak-funding unwind and permit funding-harvest logic.
4. If the requested exit is not fillable and profitable under its rule, hold.

The configured `0.50%` near-flat basis threshold is recorded for diagnostics,
but with a known next funding rate the funding hierarchy above determines the
post-funding unwind path first.

## Exit Rules

### Controlled pre-funding basis exit

Before funding, an exit is activated when executable basis improvement reaches
at least `0.75%`.

- Funding PnL is not counted because no funding has been received yet.
- The exit must make at least `0.02%` net profit excluding funding after
  allocated entry fees, depth-priced exit prices, exit fees, and safety buffer.
- The strategy evaluates `$100`, `$250`, and `$500` chunks and chooses the
  largest profitable chunk whose net percentage is within `0.05%` of the best
  available chunk.
- At least five minutes must pass between exit chunks.
- Once triggered, the position enters persistent `EXITING_PREFUNDING` state.
  No new layer is permitted for that position on a later cycle, even if basis
  temporarily moves back below the trigger.
- A final remainder no larger than `$500` may close in full when its complete
  exit is fillable and profitable.
- If no chunk passes, the position remains open. No market dump is simulated.

### Post-funding unwind

For a requested post-funding unwind:

1. It tests `$100`, `$250`, and `$500` controlled chunks and selects the largest
   profitable chunk within `0.05%` of the best net percentage.
2. At least five minutes must pass between chunks. A full close is considered
   only when the entire remainder is no larger than `$500`, fillable, and at
   least `0.02%` profitable excluding funding.
3. For weak, missing, or reversed next funding only, it can close a `$100`
   funding-harvest chunk when total profit including allocated accrued funding
   is at least `$0.25`.
4. If no full, partial, or harvest exit passes, it records
   `exit_wanted_no_profitable_chunk` and holds.

An adverse basis move never bypasses these profitability and fillability checks.

## Funding Accounting

- Actual funding is fetched from Binance settlement history after the stored
  timestamp is crossed.
- Funding PnL is calculated from current open notional and the held direction.
- Every captured event is appended to `funding_events.csv`.
- The next timestamp advances by the exchange interval, with an eight-hour
  fallback if no interval is available.
- If the process was offline across multiple settlements, it attempts to accrue
  each crossed event in sequence.
- Partial exits allocate and remove the corresponding share of accrued funding,
  quantities, notional, and entry fees from the open position.

## Data And Audit Files

All files are under `data/binance_extreme_funding/`:

| File | Purpose |
| --- | --- |
| `snapshots/snapshots_YYYYMMDD.csv` | Full funding-window observations |
| `extreme_observations/extreme_observations_YYYYMMDD.csv` | Compact settlement-reconciliation source |
| `latest_snapshots.csv` | Latest funding snapshot for every contract |
| `opportunities/opportunities_YYYYMMDD.csv` | Depth-priced rows by notional |
| `latest_opportunities.csv` | Fresh strategy input |
| `basis_history.csv` | Rolling basis observations |
| `settlement_comparisons.csv` | Displayed versus actual funding |
| `paper/signals.csv` | Event activation state |
| `paper/positions.csv` | Aggregated open and closed paper positions |
| `paper/fills.csv` | Entry, partial exit, and full exit ledger |
| `paper/decisions.csv` | Allowed and rejected entry decisions |
| `paper/funding_events.csv` | Actual funding accrual ledger |
| `paper/cooldowns.csv` | Volatility and post-close cooldowns |

The dashboard's Daily PnL tab groups realised exit PnL by UTC day. Funding
accrual is displayed alongside it as a separate informational amount because a
later exit already includes its allocated funding; the two columns must not be
added together.

## Paper And Live Limitations

- Negative funding requires a short spot leg. The paper model does not verify
  margin borrow availability, borrow interest, recalls, or liquidation risk.
- Account-specific fee tier, rebates, precision, minimum notional, and order-rate
  limits must be validated before live execution.
- Public REST books are snapshots, not atomic cross-market execution guarantees.
- The V2 paper haircuts are provisional estimates from the first 13 settled
  extreme events. They require recalibration as each lead-time bucket grows.
- This package intentionally has no authenticated client or order-placement path.

## Commands

```bash
python -m binance_extreme_funding.run_scanner
python -m binance_extreme_funding.run_backfill_extreme_observations
python -m binance_extreme_funding.run_paper_strategy
python -m binance_extreme_funding.run_dashboard --port 8770
python -m binance_extreme_funding.print_summary
python test_extreme_funding_strategies.py
```
