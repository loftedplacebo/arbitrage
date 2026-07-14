# MEXC Extreme Funding

Independent, public-data-only scanner and paper strategy for MEXC USDT
perpetuals with an absolute displayed funding rate of at least 0.50%.

The live MEXC API is the source of truth. This package does not read
`data/funding_lock_research/` or import another exchange strategy.

## Behaviour

- Captures the full MEXC perpetual ticker set every minute and enriches extreme
  events from the contract funding endpoint.
- Captures mark/index basis and matching spot/perpetual executable basis.
- Reconciles every extreme-rate observation with public settled funding history.
- Starts after two consistent observations spanning at least one minute and adds at most one paper layer
  every 30 minutes using `$50 / $100 / $250 / $500` nominal amounts.
- Exits early when basis improvement reaches 0.75% and the executable paper
  result is profitable, or on a profitable funding-sign reversal.
- Uses a 1.00% adverse-basis guard and writes only under
  `data/mexc_extreme_funding/`.

## Run

```bash
python -m mexc_extreme_funding.run_scanner
python -m mexc_extreme_funding.run_paper_strategy
python -m mexc_extreme_funding.run_dashboard --port 8771
python -m mexc_extreme_funding.print_summary
```

All execution is paper-only. No API keys or order endpoints are used.
