# Independent Extreme-Funding Paper Strategies

Binance and MEXC are deployed as separate applications because their funding
behaviour, liquidity, API shape, and nominal risk limits differ. Neither package
depends on the funding-lock research CSVs. The research collector remains an
offline calibration and audit dataset; live exchange APIs feed these strategies.

## Package and data boundaries

| Exchange | Package | Data root | Dashboard |
| --- | --- | --- | --- |
| Binance | `binance_extreme_funding/` | `data/binance_extreme_funding/` | `127.0.0.1:8770` |
| MEXC | `mexc_extreme_funding/` | `data/mexc_extreme_funding/` | `127.0.0.1:8771` |

Each data root contains daily full-window snapshots, a latest snapshot, settled
displayed-versus-actual comparisons, signal state, positions, fills, decisions,
and funding events.

## Paper rules

Both strategies require an absolute displayed rate of 0.50%, at least 15 minutes
before funding, a matching spot market, executable top-of-book basis, and two
consistent observations spanning at least one minute. A maximum of one new layer is opened per 30-minute
interval across the funding window. Closed layers are not reopened for the same
event.

Basis PnL is measured from executable top-of-book sides in the hedged direction,
so entry and exit spread are included. A layer exits before funding when basis
improvement reaches 0.75% and the result remains positive after estimated
round-trip fees. It can also exit on a profitable funding-sign reversal. Actual
settled rates are fetched from each exchange's public funding history.

## VPS deployment

Install or update only the six new services:

```bash
cd /root/arbitrage
bash scripts/install_extreme_funding_services.sh
```

The installer does not install dependencies and does not restart any existing
scanner, KuCoin strategy, or funding-lock research unit.

Open all three funding dashboards through one SSH session:

```bash
ssh \
  -L 8766:127.0.0.1:8766 \
  -L 8770:127.0.0.1:8770 \
  -L 8771:127.0.0.1:8771 \
  blockdag
```

Then open `http://127.0.0.1:8766/`, `http://127.0.0.1:8770/`, and
`http://127.0.0.1:8771/` locally.
