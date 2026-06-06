from pathlib import Path
import pandas as pd


REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = REPO_ROOT / "data" / "validated_futures_futures_snapshots"
files = sorted(DATA_DIR.glob("validated_futures_futures_*.csv"))

if not files:
    raise SystemExit("No validated futures-futures CSV files found.")

path = files[-1]
print(f"Reading: {path}")

df = pd.read_csv(path)

# Normalise booleans
for col in ["paper_ready", "spread_ready", "funding_adjusted_ready", "persistent"]:
    if col in df.columns:
        df[col] = df[col].astype(str).str.lower().isin(["true", "1", "yes"])

# Basic shape
print("\nRows:", len(df))
print("Scans:", df["timestamp_utc"].nunique())
print("Symbols:", df["symbol"].nunique())
print("Directions:", df["direction"].nunique())

# Paper-ready only
ready = df[df["paper_ready"] == True].copy()
print("\nPaper-ready rows:", len(ready))
print("Paper-ready scans:", ready["timestamp_utc"].nunique())
print("Paper-ready symbols:", ready["symbol"].nunique())

if ready.empty:
    print("\nNo paper-ready trades found.")
    raise SystemExit()

# Group persistence by symbol/direction/notional
group_cols = ["symbol", "instrument_class", "direction", "notional_usdt"]

summary = (
    ready.groupby(group_cols)
    .agg(
        scans_ready=("timestamp_utc", "nunique"),
        avg_net_ex_funding_pct=("net_edge_ex_funding_pct", "mean"),
        avg_net_inc_funding_pct=("net_edge_inc_funding_pct", "mean"),
        min_net_inc_funding_pct=("net_edge_inc_funding_pct", "min"),
        max_net_inc_funding_pct=("net_edge_inc_funding_pct", "max"),
        avg_slippage_pct=("slippage_pct", "mean"),
        avg_validated_spread_pct=("validated_spread_pct", "mean"),
        first_seen=("timestamp_utc", "min"),
        last_seen=("timestamp_utc", "max"),
    )
    .reset_index()
)

summary = summary.sort_values(
    ["scans_ready", "avg_net_inc_funding_pct"],
    ascending=[False, False],
)

print("\nTop persistent paper-ready opportunities:")
print(summary.head(30).to_string(index=False))

# Capacity view
capacity = (
    ready.groupby(["symbol", "instrument_class", "direction"])
    .agg(
        scans_ready=("timestamp_utc", "nunique"),
        max_ready_notional=("notional_usdt", "max"),
        avg_net_inc_funding_pct=("net_edge_inc_funding_pct", "mean"),
        min_net_inc_funding_pct=("net_edge_inc_funding_pct", "min"),
        avg_net_ex_funding_pct=("net_edge_ex_funding_pct", "mean"),
    )
    .reset_index()
    .sort_values(["scans_ready", "max_ready_notional", "avg_net_inc_funding_pct"], ascending=[False, False, False])
)

print("\nCapacity summary:")
print(capacity.head(30).to_string(index=False))