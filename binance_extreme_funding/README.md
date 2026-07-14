# Binance Extreme Funding

Independent, public-data-only scanner and paper strategy for Binance USDT
perpetuals with an absolute displayed funding rate of at least 0.50%.

The live Binance API is the source of truth. This package does not read
`data/funding_lock_research/` or import another exchange strategy.

## Behaviour

- Captures all Binance USDT perpetual displayed rates every minute.
- Captures mark/index basis plus 100-level spot and perpetual books for extreme
  candidates and open-position watch rows.
- Reconciles every extreme-rate observation with the public settled funding
  history after the event.
- Prices entry and exit across all four legs for each configured notional and
  requires the round trip to be fillable with positive expected edge.
- Starts after two consistent observations spanning at least one minute and
  aggregates `$100 / $250 / $500 / $1,000` layers into one weighted position.
- Holds through adverse basis moves and continues layering when funding, depth,
  basis-percentile, volatility, and portfolio checks still pass.
- Normally holds until the first funding payment, but may begin a controlled
  pre-funding unwind when executable basis improvement reaches 0.75% and the
  spread result is profitable after all modeled costs. It exits at most one
  profitable `$100 / $250 / $500` chunk per strategy cycle and blocks a new
  layer in that cycle; only the final remainder is closed as a whole. After
  funding it holds rates of at least 1.00%, attempts profitable gentle unwinds
  below that level, and can use accrued funding to harvest a profitable `$100`
  chunk when the next rate falls below 0.30% or reverses.
- Has no adverse-basis stop and no time-based forced exit. An unprofitable or
  unfillable exit request remains open.
- Writes only under `data/binance_extreme_funding/`.

`latest_snapshots.csv` remains the funding audit feed. The independent strategy
source of truth is `latest_opportunities.csv`, generated directly from Binance
funding data and Binance order books; it does not read funding-lock research CSVs.

## Run

```bash
python -m binance_extreme_funding.run_scanner
python -m binance_extreme_funding.run_paper_strategy
python -m binance_extreme_funding.run_dashboard --port 8770
python -m binance_extreme_funding.print_summary
```

All execution is paper-only. No API keys or order endpoints are used.
