# Binance Extreme Funding

Independent, public-data-only scanner and paper strategy for Binance USDT
perpetuals with an absolute displayed funding rate of at least 0.50%.

The live Binance API is the source of truth. This package does not read
`data/funding_lock_research/` or import another exchange strategy.

## Behaviour

- Captures all Binance USDT perpetual displayed rates every minute.
- Captures mark/index basis and matching spot/perpetual executable basis.
- Reconciles every extreme-rate observation with the public settled funding
  history after the event.
- Starts after two consistent observations spanning at least one minute and adds at most one paper layer
  every 30 minutes using `$100 / $250 / $500 / $1,000` nominal amounts.
- Exits early when basis improvement reaches 0.75% and the executable paper
  result is profitable, or on a profitable funding-sign reversal.
- Writes only under `data/binance_extreme_funding/`.

## Run

```bash
python -m binance_extreme_funding.run_scanner
python -m binance_extreme_funding.run_paper_strategy
python -m binance_extreme_funding.run_dashboard --port 8770
python -m binance_extreme_funding.print_summary
```

All execution is paper-only. No API keys or order endpoints are used.
