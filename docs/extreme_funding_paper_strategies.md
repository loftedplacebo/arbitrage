# Binance, MEXC, And KuCoin Funding Strategies

This is the repository-level operating specification for the three independent
spot/perpetual funding paper strategies. Exchange package READMEs remain the
authority for implementation details:

- Binance: `binance_extreme_funding/README.md`
- MEXC: `mexc_extreme_funding/README.md`
- KuCoin: `kucoin-basis-funding-arb/kucoin_basis/README.md` in the independent
  KuCoin repository

The strategies share a risk philosophy, but they do not share scanner state,
positions, funding ledgers, nominal amounts, or runtime processes.

## Ownership And Deployment

| Exchange | Code checkout | Data root | Dashboard | Scanner service | Strategy service |
| --- | --- | --- | --- | --- | --- |
| Binance | `/root/arbitrage/binance_extreme_funding/` | `/root/arbitrage/data/binance_extreme_funding/` | `127.0.0.1:8770` | `binance-extreme-funding-scanner` | `binance-extreme-funding-strategy` |
| MEXC | `/root/arbitrage/mexc_extreme_funding/` | `/root/arbitrage/data/mexc_extreme_funding/` | `127.0.0.1:8771` | `mexc-extreme-funding-scanner` | `mexc-extreme-funding-strategy` |
| KuCoin | `/opt/kucoin-basis-funding-arb/kucoin_basis/` | `/opt/kucoin-basis-funding-arb/data/kucoin_basis/` | `127.0.0.1:8766` | `kucoin-basis-scanner` | `kucoin-basis-strategy` |

Binance and MEXC are in the main Arbitrage repository. KuCoin has its own Git
repository, tests, services, and deployment path. Updating one checkout must not
restart or replace another exchange unless explicitly requested.

## Shared Direction Model

| Funding sign | Funding-receiving hedge |
| --- | --- |
| Positive | Long spot and short perpetual |
| Negative | Short spot and long perpetual |

All three define basis as perpetual price divided by spot price minus one.
Positive-funding basis improves when it falls; negative-funding basis improves
when it rises.

The short-spot direction is paper modeling. Live use requires margin borrow
availability and borrow-cost checks that are not currently implemented.

## Key Differences

| Rule | Binance | MEXC | KuCoin |
| --- | ---: | ---: | ---: |
| Nominal funding floor | `0.50%` absolute | `0.50%` absolute | `0.03%` directional |
| Practical fixed-cost funding floor before slippage | `0.35%` for `0.02%` edge, but `0.50%` trigger dominates | `0.34%` for `0.02%` edge, but `0.50%` trigger dominates | About `0.37%` |
| Minimum time before funding | 15 minutes | 15 minutes | 15 minutes |
| Signal persistence | Continuous streak: 2 observations/2 minutes; larger layers require 10/30/60 minutes | 2 observations and 1 minute | No equivalent signal-age gate |
| Scanner cadence | 60 seconds | 60 seconds | 60 seconds |
| Entry chunks | `$100/$250/$500/$1,000` | `$50/$100/$250/$500` | `$100/$250/$500/$1,000` |
| Layer interpretation | Finite ordered four-step ladder | Finite ordered four-step ladder | Best chunk menu, repeatable each fresh tick |
| Symbol cap | `$5,000` | `$2,000` | `$5,000` |
| Total cap | `$20,000` | `$10,000` | `$50,000` |
| Position-count cap | 40 | 30 | 100 |
| Pre-funding basis exit | Controlled chunks at `0.75%` improvement | Controlled chunks at `0.75%` improvement | None; hold until funding |
| Adverse basis exit | Never | Never | Never |
| Adverse basis add behavior | Can layer if all gates pass | Can layer if all gates pass | Blocks adds after `5.00%` adverse move |
| Juicy next-funding hold | `>=1.00%` | `>=1.00%` | `>=1.00%` |
| Weak next funding | `<0.30%`, missing, or reversed | `<0.30%`, missing, or reversed | Known directional benefit `<0.30%` |
| Time-based exit | None | None | None |

The practical fixed-cost floor is funding benefit minus modeled fixed costs
needed to leave `0.02%` expected edge before measured slippage. Every strategy
still performs the complete depth-priced calculation.

## Shared Scanner Invariants

All three strategies require:

1. A funding-receiving direction.
2. A matching spot and perpetual market.
3. Sufficient time before funding.
4. Fillable spot entry, perpetual entry, spot exit, and perpetual exit.
5. Positive expected edge after measured slippage, fees, and safety allowance.
6. Fresh market rows.
7. Directionally favorable basis once enough history exists.
8. Portfolio exposure capacity.

All scanners keep open positions on a watchlist so an exit can still be priced
after the original entry signal disappears.

Binance and MEXC use their own `latest_opportunities.csv`. KuCoin reads the latest
daily opportunity file from its independent data root. None consumes
`data/funding_lock_research/` for strategy decisions.

## Binance Decision Lifecycle

### Activation and entry

1. Absolute funding is at least `0.50%` and at least 15 minutes remain.
2. Four-leg expected edge is at least `0.02%`.
3. Exit cost is no more than `1.00%`.
4. After five basis observations, positive funding requires the 75th percentile
   or higher; negative funding requires the 25th percentile or lower.
5. The event remains continuously eligible for two observations over at least
   two minutes. Any drop, reversal, stale event, or gap over 180 seconds resets
   the streak.
6. The row is from the newest timestamp and no older than 180 seconds.
7. Spot/perpetual depth is no more than one second old or one second apart.
8. The next ordered layer passes its time, streak, conservative-edge, exposure,
   and cooldown rules.

The ordered layers are `$100`, `$250`, `$500`, then `$1,000`. The probe needs a
two-minute streak. Later layers require, respectively: ten minutes and at most
120 minutes to funding; 30 minutes and at most 60 minutes; 60 minutes and at
most 30 minutes. Layers above the probe subtract provisional funding-prediction
haircuts of `0.40%`, `0.20%`, `0.10%`, or `0.06%` according to lead time. The
last two require `0.10%` conservative edge; the `$250` layer requires `0.02%`.

Layers are aggregated. An adverse basis move is allowed to layer when the new
layer remains depth-priced and profitable. Basis standard deviation above
`0.75%` or absolute trend above `2.00%` pauses entries for 60 minutes.

### Hold and exit

- Before funding, hold unless basis improvement reaches `0.75%` and a profitable
  executable chunk exists.
- The pre-funding unwind enters persistent `EXITING_PREFUNDING` state, blocks
  all later re-layering, and removes at most one chunk every five minutes.
- It selects the largest profitable `$100/$250/$500` chunk within `0.05%` of
  the best chunk's net return. A remainder no larger than `$500` may close.
- After funding, hold next directional funding of `1.00%` or more.
- From `0.30%` to below `1.00%`, request a profitable unwind.
- Below `0.30%`, missing, or reversed, request a weak-funding unwind.
- Post-funding uses the same controlled five-minute chunk cadence and selection
  rule. A full close is allowed only when the remainder is no larger than
  `$500` and makes at least `0.02%` excluding funding.
- Weak-funding paths may harvest `$100` when allocated total PnL including
  funding is at least `$0.25`.
- If no exit passes, hold regardless of basis or age.

## MEXC Decision Lifecycle

### Activation and entry

MEXC uses the same gate order as Binance, with these exchange-specific rules:

- Ordered layers are `$50`, `$100`, `$250`, then `$500`.
- Symbol cap is `$2,000`; total cap is `$10,000`; position cap is 30.
- MEXC `collectCycle` supplies the funding interval.
- Futures depth volume is contract count. It is multiplied by the contract's
  `contractSize` before slippage or hedge quantity is calculated.
- Requests are paced by 0.11 seconds.
- Missing contract metadata rejects the opportunity.

### Hold and exit

- Before funding, basis improvement of `0.75%` activates one best profitable
  `$50/$100/$250` chunk per cycle.
- After funding, the `1.00%` juicy hold and `0.30%` weak-funding thresholds match
  Binance.
- Full post-funding exits require `0.02%` profit excluding funding.
- Partial exits use `$50/$100/$250`.
- Weak-funding harvest uses `$50` and requires at least `$0.15` total allocated
  profit including funding.
- Adverse basis never forces an exit.

## KuCoin Decision Lifecycle

### Activation and entry

1. Directional funding benefit is at least `0.03%`.
2. Funding benefit minus the `0.35%` fixed fee/safety allowance is at least
   `0.02%` before depth is fetched.
3. After four measured slippage components, final expected edge is still at
   least `0.02%`.
4. At least 15 minutes remain and the round trip is fillable.
5. Exit cost is no more than `1.00%`.
6. After five basis observations, the same 75th/25th directional percentile
   rules apply.
7. The row is newest and no older than 180 seconds.

KuCoin selects one highest-edge chunk per base/direction/timestamp; smaller wins
an exact edge tie. It may add another best chunk on every fresh scanner tick
until caps are reached. A `5.00%` adverse move or excessive basis volatility
holds the position, blocks additions, and starts a 60-minute cooldown.

### Hold and exit

- Before first funding, always hold. KuCoin has no pre-funding basis take-profit.
- After funding, next benefit at or above `1.00%` forces a hold.
- From `0.30%` to below `1.00%`, force a profitable gentle partial unwind using
  `$100/$250/$500`; do not test a full close first.
- Known next benefit below `0.30%` requests an unwind.
- Weak holding edge can close the full position when fillable and profitable by
  at least `0.02%` excluding funding; otherwise use the best profitable partial.
- A `$100` funding harvest is allowed only when basis/fee PnL is negative but
  total allocated PnL including funding is at least `$0.25`.
- No profitable chunk means hold.

## Cost Assumptions

| Component | Binance | MEXC | KuCoin |
| --- | ---: | ---: | ---: |
| Spot entry fee | `0.10%` | `0.10%` | `0.10%` |
| Perpetual entry fee | `0.05%` | `0.02%` | `0.06%` |
| Combined exit fee | `0.15%` | `0.17%` | `0.16%` |
| Safety buffer | `0.03%` | `0.03%` | `0.03%` |
| Fixed total | `0.33%` | `0.32%` | `0.35%` |

Measured depth slippage is added separately for all four legs. These are
conservative paper assumptions, not guarantees of a live account fee tier.

## Funding Accounting

All strategies:

- Store the predicted/displayed rate at entry.
- Fetch actual settlement history after the funding timestamp.
- Book funding using open notional and held direction.
- Advance to the next exchange interval, with an eight-hour fallback.
- Can catch up across multiple crossed settlements after downtime.
- Allocate accrued funding proportionally when a partial exit reduces notional.

## Failure And Pause Behavior

The default response to uncertainty is hold or reject, not forced execution:

- Stale opportunity: no new entry and no stale-price exit.
- Missing book or incomplete round trip: reject that notional.
- Expected edge below threshold: reject or pause layering.
- Exposure cap: reject the layer.
- Volatility cooldown: hold existing position and block new exposure.
- Exit requested but unprofitable: hold and record the reason.
- Funding-history result unavailable: retry on a later strategy pass.

## Dashboards And Tunnel

Use one SSH session for all three dashboards:

```bash
ssh \
  -L 8766:127.0.0.1:8766 \
  -L 8770:127.0.0.1:8770 \
  -L 8771:127.0.0.1:8771 \
  blockdag
```

Then open:

- KuCoin: `http://127.0.0.1:8766/`
- Binance: `http://127.0.0.1:8770/`
- MEXC: `http://127.0.0.1:8771/`

## Operational Checks

```bash
systemctl is-active \
  kucoin-basis-scanner kucoin-basis-strategy kucoin-basis-dashboard \
  binance-extreme-funding-scanner binance-extreme-funding-strategy binance-extreme-funding-dashboard \
  mexc-extreme-funding-scanner mexc-extreme-funding-strategy mexc-extreme-funding-dashboard
```

Before deleting or resetting state, stop only the target exchange's scanner,
strategy, and dashboard, verify the resolved data path, and leave the other
exchange services running.

## Live-Trading Gaps

None of these packages is ready to submit live orders. A live implementation
still needs:

- Authenticated clients and idempotent order state.
- Spot borrow availability, cost, recall, and margin controls.
- Exchange-specific quantity/price precision and minimum-size validation.
- Account fee tiers and rebates.
- Atomic or failure-aware two-leg execution.
- Partial-fill recovery and hedge rebalancing.
- Liquidation, collateral, and exchange-outage controls.
- Reconciliation against account balances, fills, and actual funding payments.
