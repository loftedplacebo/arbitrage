# MEXC Extreme-Funding V2 Paper Strategy

This package is an independent public-data scanner, paper ledger, strategy, and
dashboard for MEXC USDT perpetual funding events. It does not consume
`data/funding_lock_research/`, share state with Binance or KuCoin, use API keys,
or submit orders.

V2 keeps the KuCoin-style hold and controlled-exit philosophy, but adds
MEXC-specific capability checks, integer contract sizing, timed confidence
layers, continuous signal streaks, fair-price funding accounting, and compact
settlement reconciliation.

## Activation

| Process | Command | VPS service | Interval/port |
| --- | --- | --- | --- |
| Scanner | `python -m mexc_extreme_funding.run_scanner --loop` | `mexc-extreme-funding-scanner` | 60 seconds after each pass |
| Paper strategy | `python -m mexc_extreme_funding.run_paper_strategy --loop` | `mexc-extreme-funding-strategy` | 60 seconds |
| Dashboard | `python -m mexc_extreme_funding.run_dashboard --port 8771` | `mexc-extreme-funding-dashboard` | `127.0.0.1:8771` |

Starting the services does not activate a position. Every funding, capability,
contract, depth, timing, basis, volatility, edge, lifecycle, and portfolio gate
below must pass independently.

## Direction And Executability

| Displayed funding | Hedge | Funding receiver | Default V2 status |
| --- | --- | --- | --- |
| Positive | Long spot, short perpetual | Short perpetual | Eligible when all other gates pass |
| Negative | Short spot, long perpetual | Long perpetual | Requires authenticated free base-asset inventory sufficient for the hedge |

MEXC `exchangeInfo` remains the market-structure source, while signed Spot v3
account data is the capacity source. V2 records `spot_buy_available`,
`short_spot_available`, and `perp_api_allowed` on every depth-priced opportunity.
It requires free USDT plus a 2% reserve for a positive-funding spot buy, or free
base inventory for a negative-funding spot sale. MEXC's documented Spot v3 API
does not expose a max-borrowable cross/isolated-margin endpoint, so a public
`isMarginTradingAllowed` flag is never treated as proof that a short can be
borrowed. Unfunded events remain research rows and cannot open.

`inventory_backed_short_spot_symbols` is empty by default. Adding a symbol is an
explicit paper assumption that sufficient spot inventory is reserved for the
entire short-and-buyback cycle. It is not an authenticated balance check.

## Scanner And Source Of Truth

Every pass:

1. Downloads all MEXC USDT perpetual tickers and spot top of book.
2. For rates with absolute value at least `0.50%`, fetches the detailed current
   funding rate, `nextSettleTime`, and `collectCycle`.
3. Records current/predicted funding, next settlement, mark/index basis, matching
   spot market, and executable top-of-book basis.
4. Fetches and caches spot `exchangeInfo` plus perpetual contract metadata.
5. For extreme candidates and open-position watch rows, downloads 100 levels of
   spot and perpetual depth.
6. Rejects books older than two seconds or with more than two seconds of
   cross-market observation skew.
7. Converts MEXC contract volume into base quantity using `contractSize`, then
   prices the same rounded base quantity through spot and perpetual entry and
   exit books.
8. Writes full snapshots, compact extreme observations, rolling basis history,
   and notional-specific opportunity rows.
9. Reconciles compact extreme observations against actual settlement history.

`latest_opportunities.csv` is the strategy execution source of truth.
`latest_snapshots.csv` is the live research feed. The scanner no longer rereads
the large full-market snapshot archives every minute.

## Contract And Hedge Rules

Before an opportunity is executable:

- Spot API trading must be enabled.
- The perpetual must have `state == 0` and `apiAllowed == true`.
- `contractSize`, `volUnit`, `minVol`, and `maxVol` must be present.
- Contract count is rounded down to `volUnit` and kept within the exchange
  minimum and maximum.
- The resulting base quantity must also satisfy the spot quantity step.
- Exactly the same base quantity is priced on both legs.
- Residual delta must not exceed `0.25%` of requested notional.
- All four rounded-quantity legs must be fillable.

The requested ladder notional remains the portfolio budget and row key. Actual
spot and perpetual entry notionals are stored separately for fees and audit.

## Entry Rules

### Funding and market gate

- Absolute displayed funding is at least `0.50%`.
- At least seven minutes remain before settlement.
- Funding sign determines the hedge direction.
- Matching spot and perpetual markets and valid prices exist.
- A negative-rate trade passes the short-spot capability rule above.

### Depth and edge gate

Expected edge is:

```text
directional displayed funding benefit
- spot entry slippage
- perpetual entry slippage
- spot exit slippage
- perpetual exit slippage
- 0.10% modeled spot entry fee
- 0.02% modeled perpetual entry fee
- 0.17% modeled combined exit fee
- 0.03% safety buffer
```

- The fixed modeled cost is `0.32%` before measured slippage.
- Raw expected edge must be at least `0.10%`.
- Exit slippage plus modeled exit fees must not exceed `0.60%`.
- The opportunity must come from the newest scanner timestamp and be no more
  than 180 seconds old.

### Basis and volatility gate

The latest 15 basis observations are retained. After five observations:

- `LONG_SPOT_SHORT_PERP` requires the 75th basis percentile or higher.
- `SHORT_SPOT_LONG_PERP` requires the 25th percentile or lower.
- Basis standard deviation above `0.75%`, or absolute lookback trend above
  `2.00%`, starts a 60-minute entry cooldown.

Orderly adverse basis can improve a later entry. Volatility pauses new risk but
never forces an existing hedge to close.

### Continuous signal gate

- The same event key, funding timestamp, symbol, and direction must remain
  extreme for three consecutive fresh scanner observations.
- Consecutive observations may be no more than 90 seconds apart.
- A below-threshold rate, direction change, stale/missing snapshot, settlement,
  or longer gap resets the active streak.
- Lifetime observations remain available for research but cannot reactivate a
  broken streak.
- Reprocessing the same scanner timestamp does not increment the streak.

### Timed layer gate

| Layer | Notional | Maximum time to funding | Minimum continuous signal age | Minimum conservative edge |
| --- | ---: | ---: | ---: | ---: |
| Probe | `$50` | 120 minutes | 2 minutes | `0.25%` |
| Layer 2 | `$100` | 60 minutes | 15 minutes | `0.20%` |
| Layer 3 | `$250` | 30 minutes | 30 minutes | `0.15%` |
| Layer 4 | `$500` | 12 minutes | 45 minutes | `0.10%` |

- Layers are finite and used in order.
- At least five minutes must pass between layers.
- At most one layer per base/direction can be added in one strategy cycle.
- Later layers use the minimum rate in the current streak and subtract a
  lead-time haircut: `0.25%` beyond 60 minutes, `0.20%` beyond 30, `0.12%`
  beyond 15, and `0.05%` beyond seven.
- A late signal cannot compress all four layers into a few cycles because each
  later layer also requires its increasing continuous-signal age.
- Any position in a controlled exit state blocks every additional layer.

Portfolio caps remain `$2,000` per symbol, `$10,000` total open notional, and 30
aggregated positions. Opposite-direction exposure in the same base is blocked.

## Hold Rules

There is no adverse-basis stop and no maximum holding time.

Before first funding:

- Hold through flat or adverse basis.
- Continue considering the next timed layer while every entry gate passes.
- Missing, stale, unfillable, volatile, or capability-invalid data pauses entry.
- The only exit is the profitable controlled basis exit below.

After first funding:

1. Hold when the next directional funding benefit is at least `1.00%`, unless a
   controlled post-funding exit has already begun.
2. From `0.30%` to below `1.00%`, start a profitable gentle unwind.
3. If next funding is missing, below `0.30%`, or reversed, start a weak-funding
   unwind and allow funding-harvest chunks.
4. Once post-funding exit begins, it remains `EXITING_POSTFUNDING`; new layers
   cannot restart the position.
5. If no fillable profitable chunk exists, hold.
6. Eight hours after post-funding unwinding begins, a fillable remainder no larger
   than `$250` may close at no worse than `-$0.15` total PnL. This is a bounded
   tail rule, not an adverse-basis stop.

## Exit Rules

### Pre-funding basis exit

- Activates at `0.75%` executable basis improvement.
- Requires at least `0.02%` profit excluding funding after entry fees,
  depth-priced exit, exit fees, and safety buffer.
- Evaluates `$50/$100/$250` chunks.
- Chooses the largest profitable chunk within `0.05%` of the best net percentage.
- Removes at most one chunk every five minutes.
- Persists `EXITING_PREFUNDING` after the first trigger and blocks all re-entry.
- A final remainder no larger than `$250` can close in full when profitable and
  fillable.
- An adverse move never causes an uncontrolled exit.

### Post-funding unwind

- Uses the same largest-near-best chunk rule and five-minute pacing.
- Full closure is considered only when the remainder is at most `$250` and earns
  at least `0.02%` excluding funding.
- On weak next funding, that same small remainder may close when total PnL,
  including captured funding, is at least `$0.15`.
- Weak, missing, or reversed next funding may harvest a `$50` chunk when total
  allocated profit including funding is at least `$0.15`.
- No qualifying chunk means `exit_wanted_no_profitable_chunk` and continued hold.

## Funding Accounting

- Actual funding rate is fetched from MEXC settlement history.
- Settlement fair price is fetched from the one-minute fair-price series; the
  latest mark or entry price is a fallback if the public series is unavailable.
- Funding notional is open perpetual base quantity multiplied by settlement fair
  price, not the original requested USD amount.
- Every event stores actual rate, fair price, funding notional, benefit, and PnL.
- `collectCycle` advances the next timestamp; eight hours is fallback only.
- Partial exits proportionally allocate quantities, fees, and accrued funding.

## Data And Migration

All files are under `data/mexc_extreme_funding/`:

| File | Purpose |
| --- | --- |
| `snapshots/snapshots_YYYYMMDD.csv` | Full funding-window research archive |
| `extreme_observations/extreme_observations_YYYYMMDD.csv` | Compact settlement source |
| `latest_snapshots.csv` | Latest funding state |
| `opportunities/opportunities_YYYYMMDD.csv` | Contract-aware depth rows |
| `latest_opportunities.csv` | Fresh strategy input |
| `basis_history.csv` | Rolling basis observations |
| `settlement_comparisons.csv` | Displayed versus actual funding |
| `paper/signals.csv` | Lifetime and continuous streak state |
| `paper/positions.csv` | Aggregated positions and lifecycle state |
| `paper/fills.csv` | Entry and controlled-exit ledger |
| `paper/decisions.csv` | Entry allow/reject audit |
| `paper/funding_events.csv` | Fair-price funding ledger |
| `paper/cooldowns.csv` | Volatility and post-close cooldowns |

The dashboard's Daily PnL tab groups exits by UTC day and separates realised
price/basis PnL (after entry and exit costs) from the funding allocated to that
exit. `Total realised` is their sum. Open mark-to-market PnL is displayed
separately and is never included in the daily total. Older fill rows created
before this split appear as `Legacy unattributed` rather than being guessed.

CSV headers migrate when first rewritten. Existing open positions retain their
stored quantities and continue to be managed. Old rows with no execution
quantities remain open but are marked `legacy_position_missing_execution_quantities`
and cannot be repriced, funded, layered, or exited automatically.

## Commands

```bash
python -m mexc_extreme_funding.run_scanner
python -m mexc_extreme_funding.run_backfill_extreme_observations
python -m mexc_extreme_funding.run_paper_strategy
python -m mexc_extreme_funding.run_dashboard --port 8771
python -m mexc_extreme_funding.print_summary
python test_extreme_funding_strategies.py
```

The package remains deliberately paper-only. It reads authenticated balances but
does not borrow or trade. A future executor must add MEXC margin-borrow controls
once MEXC exposes a documented borrow-limit API, then revalidate fees,
price/quantity precision, order limits, and both-leg execution risk.
