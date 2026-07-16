# MEXC Extreme-Funding Paper Strategy

This package is an independent public-data scanner, paper ledger, strategy, and
dashboard for MEXC USDT perpetual funding events. It does not import Binance or
KuCoin strategy code, read funding-lock research CSVs, use API keys, or submit
orders.

MEXC uses the same tested hold and controlled-unwind philosophy as Binance, but
keeps its own nominal ladder, exposure limits, fee assumptions, symbol mapping,
funding interval, API pacing, and futures contract multiplier handling.

## Activation

| Process | Command | VPS service | Interval/port |
| --- | --- | --- | --- |
| Scanner | `python -m mexc_extreme_funding.run_scanner --loop` | `mexc-extreme-funding-scanner` | 60 seconds |
| Paper strategy | `python -m mexc_extreme_funding.run_paper_strategy --loop` | `mexc-extreme-funding-strategy` | 60 seconds |
| Dashboard | `python -m mexc_extreme_funding.run_dashboard --port 8771` | `mexc-extreme-funding-dashboard` | `127.0.0.1:8771` |

Service activation only starts data collection. A paper trade activates after
all funding, signal, depth, basis, cost, and portfolio gates pass.

## Direction And Basis

| Displayed funding | Paper hedge | Funding benefit |
| --- | --- | --- |
| Positive | Long spot, short perpetual | The short perpetual receives funding |
| Negative | Short spot, long perpetual | The long perpetual receives funding |

Basis is:

```text
basis_pct = (perpetual_price / spot_price - 1) * 100
```

Basis improvement is entry basis minus current exit basis for
`LONG_SPOT_SHORT_PERP`, and current exit basis minus entry basis for
`SHORT_SPOT_LONG_PERP`.

## MEXC Market-Data Handling

Every scanner pass:

1. Downloads the complete MEXC contract ticker set and MEXC spot top of book.
2. For rates at or above the extreme threshold, queries the contract funding
   endpoint for the detailed rate, next settlement time, and `collectCycle`.
3. Maps `BASE_USDT` perpetual symbols to `BASEUSDT` spot symbols.
4. For an entry candidate or open-position watch row, downloads 100 levels of
   spot and perpetual depth.
5. Downloads and caches MEXC contract metadata.
6. Converts futures book volume from contracts to base quantity using
   `contractSize` before estimating hedge quantity, fillability, or slippage.
7. Prices all four entry/exit legs for every relevant notional.
8. Reconciles displayed observations with actual MEXC funding history after
   settlement.

MEXC requests are deliberately paced by 0.11 seconds. A missing or invalid
contract multiplier rejects the opportunity instead of assuming one contract is
one base unit.

`latest_opportunities.csv` is the strategy's execution source of truth.
`latest_snapshots.csv` remains the funding audit feed.

## Entry Rules

### 1. Funding snapshot eligibility

- Absolute displayed funding must be at least `0.50%`.
- At least 15 minutes must remain before settlement.
- The detailed MEXC funding endpoint must provide a funding timestamp.
- A matching MEXC spot market and valid spot/perpetual prices must exist.
- Positive funding selects long spot/short perpetual; negative funding selects
  short spot/long perpetual.

### 2. Depth and expected-edge eligibility

- The tested notional must be fillable across spot entry, perpetual entry, spot
  exit, and perpetual exit.
- Expected edge after depth slippage, modeled fees, and safety buffer must be at
  least `0.02%`.
- Exit slippage plus modeled exit fees must not exceed `1.00%`.
- The strategy accepts only rows from the newest scanner timestamp, no more than
  180 seconds old.

Expected edge is:

```text
funding benefit
- four measured slippage components
- 0.10% spot entry fee
- 0.02% perpetual entry fee
- 0.17% combined exit fee allowance
- 0.03% safety buffer
```

The fixed fee/safety component is `0.32%` before measured slippage. This is why
a large displayed MEXC rate can still reject every layer except the smallest, or
reject the event entirely.

### 3. Basis-history gate

The latest 15 basis observations are retained. After five observations:

- `LONG_SPOT_SHORT_PERP` requires the 75th percentile or higher.
- `SHORT_SPOT_LONG_PERP` requires the 25th percentile or lower.
- Basis standard deviation above `5.00%`, or absolute lookback trend above
  `5.00%`, starts a 60-minute entry cooldown.

An orderly adverse widening may improve a later layer. Volatility pauses adds;
it does not close the existing hedge.

### 4. Signal activation

- The same symbol, funding timestamp, and direction must be seen twice.
- At least one minute must pass from the first observation.
- The latest observation must still be an eligible depth-priced candidate.
- Reprocessing one scanner timestamp does not increase the observation count.

### 5. Layer and portfolio gate

- Entry ladder: `$50`, `$100`, `$250`, `$500`.
- Layers are used in that order and aggregated into one weighted position.
- At most one layer is added per base/direction in a strategy cycle.
- At least one minute must pass between layers.
- Maximum symbol notional is `$2,000`.
- Maximum total strategy notional is `$10,000`.
- Maximum aggregated open positions is 30.
- An opposite-direction position in the same base blocks entry.
- Full closure starts a 60-minute re-entry cooldown.

The four entry layers are finite. A rejected larger layer does not cause the
strategy to substitute a different amount; it waits for that required layer to
become independently profitable and fillable.

## Hold Rules

MEXC has no adverse-basis stop and no maximum holding time.

Before the first funding event:

- Hold through flat or adverse basis.
- Continue considering the next layer while all entry rules remain valid.
- Pause new exposure on stale data, missing depth, failed multiplier lookup,
  excessive volatility, insufficient expected edge, or an exposure limit.
- Use only the controlled profitable basis exit described below.

After funding:

1. Hold when the next funding benefit is at least `1.00%`.
2. From `0.30%` up to but excluding `1.00%`, request a profitable unwind.
3. When next funding is missing, below `0.30%`, or reversed, request a
   weak-funding unwind and permit funding-harvest logic.
4. Hold whenever no requested exit is both fillable and profitable.

The configured `0.50%` near-flat basis threshold is retained for marking and
diagnostics. With a known next rate, the funding hierarchy is evaluated first.

## Exit Rules

### Controlled pre-funding basis exit

- Activates at `0.75%` executable basis improvement.
- Requires at least `0.02%` net profit excluding funding after allocated entry
  fees, depth-priced exit, exit fees, and safety buffer.
- Evaluates `$50`, `$100`, and `$250` chunks.
- Chooses the highest net percentage profitable chunk, breaking a tie in favor
  of the larger notional.
- Removes at most one chunk per cycle.
- Blocks same-cycle re-layering after a successful partial exit.
- Allows only a final remainder no larger than `$250` to close in full.
- Holds when no chunk is profitable and fillable.

### Post-funding unwind

1. Test a complete fillable exit and allow it when net PnL excluding funding is
   at least `0.02%`.
2. Otherwise test `$50`, `$100`, and `$250` chunks and select the best profitable
   net percentage excluding funding.
3. For missing, weak, or reversed next funding, allow a `$50` funding-harvest
   chunk when total profit including allocated funding is at least `$0.15`.
4. Otherwise record `exit_wanted_no_profitable_chunk` and hold.

No exit signal can bypass fillability or profitability because basis moved
adversely.

## Funding Accounting

- Funding uses the actual settled MEXC rate, not the displayed entry estimate.
- `collectCycle` is stored on the position; eight hours is used only as fallback.
- Each crossed settlement is accrued in sequence when the strategy resumes.
- Funding PnL uses current open notional and direction.
- Partial exits allocate funding, fees, quantities, and notional proportionally.

## Legacy Paper-State Guard

Positions written by the old schema have no stored spot/perpetual quantities or
entry prices. Such a row is marked `legacy_position_missing_execution_quantities`.
The new strategy will not add, reprice, accrue funding, or exit that row. It must
be archived or explicitly migrated. The July 2026 VPS deployment reset the old
Binance and MEXC paper data before activating this schema.

## Data And Audit Files

All files are under `data/mexc_extreme_funding/`:

| File | Purpose |
| --- | --- |
| `snapshots/snapshots_YYYYMMDD.csv` | Full funding-window observations |
| `latest_snapshots.csv` | Latest contract funding state |
| `opportunities/opportunities_YYYYMMDD.csv` | Depth-priced notional rows |
| `latest_opportunities.csv` | Fresh strategy input |
| `basis_history.csv` | Rolling basis observations |
| `settlement_comparisons.csv` | Displayed versus settled funding |
| `paper/signals.csv` | Two-observation activation state |
| `paper/positions.csv` | Aggregated paper positions |
| `paper/fills.csv` | Entry and exit ledger |
| `paper/decisions.csv` | Entry allow/reject audit |
| `paper/funding_events.csv` | Actual funding ledger |
| `paper/cooldowns.csv` | Volatility and post-close cooldowns |

## Paper And Live Limitations

- Negative funding requires spot margin borrowing. Borrow availability, borrow
  interest, recalls, margin tiers, and liquidation are not modeled.
- Live execution must enforce MEXC contract quantity precision, minimum volume,
  spot minimum notional, price precision, and current account fee tier.
- Contract metadata can change and must remain part of the live pre-trade check.
- Separate REST books are not an atomic cross-market execution guarantee.
- This package is deliberately paper-only and unauthenticated.

## Commands

```bash
python -m mexc_extreme_funding.run_scanner
python -m mexc_extreme_funding.run_paper_strategy
python -m mexc_extreme_funding.run_dashboard --port 8771
python -m mexc_extreme_funding.print_summary
python test_extreme_funding_strategies.py
```
