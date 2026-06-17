# Arbitrage

Paper-trading and research tooling for cross-exchange futures-futures spread arbitrage.

The current production path is paper-only:

- scanner: `scanners/fast_futures_futures_scanner.py`
- paper strategy loop: `strategy/run_strategy_loop.py`
- state summary: `strategy/print_strategy_summary.py`
- strategy state archive/reset: `strategy/archive_strategy_state.py`
- ML observation labelling: `analysis/build_ml_outcome_labels.py`

No live exchange execution is implemented in the strategy package. Do not add API keys to run the paper scanner or paper strategy.

## What It Does

The scanner pulls public futures market data from supported exchanges, finds cross-exchange USDT futures spreads, deep-validates selected crypto opportunities with order books and funding data, and writes CSV snapshots.

The paper strategy reads validated scanner rows and simulates entries/exits with risk controls. It records positions, slices, fills, decisions, and estimated PnL under `data/strategy/`.

The ML logger records broader fast-spread observations under `data/ml/fast_spread_observations/` so future analysis can label spread behaviour over time.

## Repository Layout

```text
Binance/                             Binance public market adapter and older tools
Bitget/                              Bitget public market adapter and older tools
Kucoin/                              KuCoin public market adapter and older tools
Mexc/                                MEXC public market adapter and older tools
core/                                shared order book and scoring helpers
scanners/fast_futures_futures_scanner.py
strategy/                            paper strategy engine
analysis/                            offline analysis scripts
docs/strategy.md                     strategy notes
data/                                generated CSV output, ignored by git
```

## Ubuntu VPS Setup

These commands assume Ubuntu 22.04/24.04 and a fresh VPS.

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip tmux
```

Clone the repo:

```bash
cd ~
git clone https://github.com/loftedplacebo/arbitrage.git
cd arbitrage
```

Create and activate a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Install Python dependencies:

```bash
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Quick sanity check:

```bash
python test_strategy_engine.py
```

Expected output:

```text
strategy engine tests passed
```

## Data Directories

Generated data is intentionally ignored by git.

```text
data/fast_futures_futures_snapshots/
data/validated_futures_futures_snapshots/
data/ml/fast_spread_observations/
data/ml/labelled_observations/
data/strategy/
```

The strategy archive script moves active paper state into:

```text
data/strategy/archive/YYYYMMDD_HHMMSS/
```

Scanner history is not archived by that script.

## Run Once

Run one scanner pass:

```bash
python scanners/fast_futures_futures_scanner.py
```

Run the paper strategy once against the latest validated scanner CSV:

```bash
python strategy/run_strategy_loop.py --latest-only
```

Print strategy state:

```bash
python strategy/print_strategy_summary.py
```

## Continuous Paper Run

Use three `tmux` panes or sessions.

Start a tmux session:

```bash
tmux new -s arbitrage
```

### Pane 1: Scanner

```bash
cd ~/arbitrage
source .venv/bin/activate
python scanners/fast_futures_futures_scanner.py --loop --interval 30
```

Optional websocket-assisted scanner mode:

```bash
python scanners/fast_futures_futures_scanner.py \
  --loop \
  --interval 30 \
  --use-websocket-cache \
  --ws-depth-cache \
  --ws-warmup-seconds 20 \
  --ws-depth-wait-seconds 2 \
  --funding-cache-seconds 240 \
  --ws-funding-reconcile-seconds 180 \
  --ws-funding-reconcile-symbol-limit 80
```

Websocket mode is hybrid and paper/data only. The scanner prefers fresh streamed
top-of-book and candidate depth data, then falls back to REST whenever a stream
is cold, stale, or missing. Binance, Bitget, MEXC, KuCoin, and Hyperliquid are
enabled by default when websocket mode is switched on. KuCoin futures websockets
use the public token/server flow internally, so no API keys are required.

Funding is also hybrid. Streamed funding fields are used when a venue provides
them reliably, and the websocket service reconciles funding from public REST
endpoints every few minutes for active/candidate symbols.

### Pane 2: Strategy Loop

```bash
cd ~/arbitrage
source .venv/bin/activate
python strategy/run_strategy_loop.py --loop --interval 30 --quiet-idle
```

### Pane 3: Summary Watch

```bash
cd ~/arbitrage
source .venv/bin/activate
watch -n 60 python strategy/print_strategy_summary.py
```

Detach from tmux:

```text
Ctrl-b d
```

Reattach:

```bash
tmux attach -t arbitrage
```

## Clean Paper Restart

Stop the scanner and strategy loop first.

Archive active strategy state:

```bash
python strategy/archive_strategy_state.py
```

Then restart the scanner and strategy loop. This does not delete scanner history.

## Offline ML Labels

The scanner writes broad fast-spread observations to:

```text
data/ml/fast_spread_observations/fast_spread_observations_YYYYMMDD.csv
```

Build future-spread labels:

```bash
python analysis/build_ml_outcome_labels.py
```

Use a specific date and horizons:

```bash
python analysis/build_ml_outcome_labels.py --input-date 20260606 --horizons 1,3,5,15,30
```

Output goes to:

```text
data/ml/labelled_observations/
```

## Current Strategy Defaults

Important defaults live in `strategy/config.py`.

Current paper-mode highlights:

```text
max_daily_entries = 500
max_open_positions = 50
max_slices_per_position = 3
max_slice_notional_usd = 500
max_total_open_notional_usd = 10000

min_validated_spread_pct = 0.75
min_net_spread_ex_funding_pct = 0.50
min_net_edge_inc_funding_pct = 0.50

funding_capture_enabled = True
funding_capture_window_minutes = 90
min_funding_benefit_for_capture_pct = 0.05
funding_capture_min_net_spread_ex_funding_pct = 0.20
funding_capture_min_net_edge_inc_funding_pct = 0.35

use_dynamic_take_profit = True
min_take_profit_pct = 0.35
take_profit_edge_fraction = 0.35
max_take_profit_pct = 1.00

stop_loss_pct = -1.00
exit_on_missing_opportunity = False
min_profit_to_exit_remaining_edge_pct = 0.05
```

## Paper-Only Safety Notes

- The strategy package does not place live trades.
- No API keys are required for the paper scanner/strategy.
- Public exchange endpoints are used for market data.
- Paper PnL is estimated from spread movement and estimated fees/slippage.
- Funding accrual is still a placeholder and is not fully realised in PnL.
- Existing scanner and strategy CSV outputs are local research artifacts, not source code.

## Git Notes

`data/` is ignored by git so large CSV logs do not get pushed.

Before committing code changes:

```bash
git status
python test_strategy_engine.py
git add .
git commit -m "Describe the change"
git push
```

## Troubleshooting

If imports fail, make sure the virtual environment is active:

```bash
source .venv/bin/activate
which python
```

If no strategy decisions appear, make sure the scanner has produced a validated CSV:

```bash
ls -lh data/validated_futures_futures_snapshots/
```

If the strategy appears idle, it may have already processed the latest scan. For a one-off replay of the latest scan:

```bash
python strategy/run_strategy_loop.py --latest-only --reprocess
```

Use `--reprocess` carefully because it can duplicate paper decisions/fills if active state is not archived first.
