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
and funding events. Binance additionally stores notional-specific, depth-priced
opportunities and basis history.

## Paper rules

Both strategies require an absolute displayed rate of 0.50%, at least 15 minutes
before funding, a matching spot market, and two consistent observations spanning
at least one minute.

### Binance

Binance follows the tested KuCoin position-management structure with
exchange-specific thresholds and nominal amounts. The scanner reads 100 levels
from both books and estimates entry and exit for all four legs. Funding, depth,
fees, slippage, basis percentile, data freshness, and risk limits must all pass
before each layer. Layers are aggregated into a weighted position.

Adverse basis is not an exit condition. A controlled adverse widening may admit
the next ladder entry when its independent expected edge remains positive. Stale
or unfillable books and disorderly basis volatility pause layering but do not
close the position.

The position normally holds until at least one funding payment. The exception is
a controlled pre-funding unwind when executable basis improvement reaches 0.75%
and spread PnL remains positive after entry fees, exit fees, slippage, and the
safety buffer. At most one profitable `$100 / $250 / $500` chunk is removed per
strategy cycle, re-layering is blocked for that cycle, and only the final small
remainder is closed in one operation. There is no maximum hold time. After
funding, a next funding benefit of at least 1.00% forces a hold; 0.30% to 1.00%
permits a profitable gentle unwind; a weaker or reversed rate permits a
profitable unwind or a small funding-harvest reduction. No exit is executed
unless the selected position or chunk is fillable and satisfies its profit rule.

### MEXC

MEXC uses the same tested position-management behavior while retaining its own
entry rules, `$50 / $100 / $250 / $500` nominal ladder, lower exposure limits,
API pacing, symbols, and exchange-reported funding interval. Futures order-book
volumes are converted from contracts to base quantity using the contract's
`contractSize` before fillability or slippage is calculated.

Adverse basis is not an exit condition. Pre-funding basis profit is unwound in
one profitable `$50 / $100 / $250` chunk per cycle, and post-funding positions
use the same hold, juicy-funding, gentle-unwind, weak-funding, repeated-settlement,
and no-profitable-exit hold rules as Binance. MEXC remains a separate package and
data root. Actual settled rates are fetched from MEXC public funding history.

Negative-funding trades require a short spot leg. The paper strategy models that
leg, but a live strategy must verify MEXC margin borrow availability, borrow cost,
quantity precision, minimum order size, and contract multiplier before entry.

Paper positions created by the earlier MEXC schema do not contain leg quantities
or execution prices. The new strategy marks them as requiring migration and will
neither accrue funding, exit, nor add layers to them. Existing VPS paper state
must be archived or explicitly migrated before this version is deployed.

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
