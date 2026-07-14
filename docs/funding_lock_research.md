# Funding Lock Research

This research job measures whether an exchange's displayed funding rate is
effectively locked before settlement.

It is separate from the scanner and both paper strategies. It uses public REST
only and writes to `data/funding_lock_research/`.

## What It Captures

For MEXC, Binance, and OKX the script can record displayed funding fields:

- `current_funding_rate`
- `predicted_funding_rate`
- `next_funding_rate`
- settlement timestamps
- basic mark/index fields when the exchange returns them

The VPS service is currently configured to collect **OKX only** so the sample
size for OKX funding-lock and basis behaviour grows faster. Binance and MEXC
historical research data is retained, but the live collector no longer spends
API budget on those exchanges.

After settlement, it fetches the exchange's historical funding endpoint and
compares each pre-settlement observation to the settled funding rate.

## Run Once

```bash
python funding_lock_research/run_funding_lock_research.py
```

Useful smoke test:

```bash
python funding_lock_research/run_funding_lock_research.py --max-symbols 3
```

## Continuous Research

```bash
python funding_lock_research/run_funding_lock_research.py \
  --loop \
  --interval 300 \
  --exchanges okx \
  --request-sleep 0.12
```

The default loop cadence is intentionally conservative. The job is read-only,
but it still shares public exchange rate limits with the scanner.

## Outputs

```text
data/funding_lock_research/snapshots/funding_rate_snapshots_YYYYMMDD.csv
data/funding_lock_research/comparisons/funding_rate_comparisons_all.csv
data/funding_lock_research/reports/funding_lock_events.csv
data/funding_lock_research/reports/funding_lock_scores.csv
```

`funding_lock_scores.csv` is the strategy-facing output. It groups by exchange,
symbol, funding field, and minutes-to-settlement bucket, then reports:

- match rate versus settled funding
- sign flip rate
- receiver reversal rate
- mean, p95, and max absolute rate error

This lets future sizing logic layer entries only where the funding estimate has
historically stayed reliable.

## Deployment

The systemd unit is `deployment/systemd/funding-lock-research.service`.

It runs from `/root/arbitrage` by default and uses the repo virtualenv:

```bash
bash scripts/install_funding_lock_research_service.sh
```

The unit is intentionally independent of the futures-futures scanner, the
futures-futures paper strategy, and the KuCoin basis services.

The install helper only enables/restarts `funding-lock-research`; it does not
touch the other services.
