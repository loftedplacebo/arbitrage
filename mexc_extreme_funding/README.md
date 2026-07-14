# MEXC Extreme Funding

Independent, public-data-only scanner and paper strategy for MEXC USDT
perpetuals with an absolute displayed funding rate of at least 0.50%.

The live MEXC API is the source of truth. This package does not read
`data/funding_lock_research/` or import another exchange strategy.

## Behaviour

- Captures the full MEXC perpetual ticker set every minute and enriches extreme
  events from the contract funding endpoint.
- Captures mark/index basis plus 100-level spot and perpetual books for extreme
  candidates and open-position watch rows. Futures contract volume is converted
  to base-asset quantity using MEXC `contractSize` metadata.
- Reconciles every extreme-rate observation with public settled funding history.
- Prices entry and exit across all four legs for every configured notional and
  requires a fillable round trip with positive expected edge.
- Starts after two consistent observations spanning at least one minute and
  aggregates `$50 / $100 / $250 / $500` layers into one weighted position.
- Holds through adverse basis moves and continues layering while the MEXC entry,
  funding, depth, basis-percentile, volatility, and portfolio rules pass.
- Uses a controlled pre-funding basis unwind at 0.75% improvement: at most one
  profitable `$50 / $100 / $250` chunk per cycle, with no same-cycle re-layer.
- Supports repeated funding capture and profitable post-funding full, partial,
  and `$50` funding-harvest exits. It has no adverse-basis or time-based stop.
- Writes only under `data/mexc_extreme_funding/`.

`latest_opportunities.csv` is generated directly from MEXC public funding,
contract metadata, and order books. The strategy remains independent of Binance,
KuCoin, and funding-lock research CSVs.

## Run

```bash
python -m mexc_extreme_funding.run_scanner
python -m mexc_extreme_funding.run_paper_strategy
python -m mexc_extreme_funding.run_dashboard --port 8771
python -m mexc_extreme_funding.print_summary
```

All execution is paper-only. No API keys or order endpoints are used.
