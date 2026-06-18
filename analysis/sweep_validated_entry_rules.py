from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from itertools import product
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
LABEL_INPUT_DIR = REPO_ROOT / "data" / "ml" / "labelled_validated_opportunities"
SWEEP_OUTPUT_DIR = REPO_ROOT / "data" / "ml" / "parameter_sweeps"
SWEEP_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def parse_float(value: str | None) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_bool(value: str | None) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes", "y"}


def parse_grid(value: str) -> list[float]:
    grid = []
    for item in value.split(","):
        item = item.strip()
        if item:
            grid.append(float(item))
    if not grid:
        raise argparse.ArgumentTypeError("Grid must contain at least one value.")
    return grid


def latest_labelled_date() -> str:
    files = sorted(LABEL_INPUT_DIR.glob("labelled_validated_opportunities_*.csv"))
    if not files:
        raise SystemExit(f"No labelled validated opportunity files found in {LABEL_INPUT_DIR}")
    return files[-1].stem.removeprefix("labelled_validated_opportunities_")


@dataclass(frozen=True)
class RuleSet:
    min_validated_spread_pct: float
    min_net_spread_ex_funding_pct: float
    min_net_edge_inc_funding_pct: float
    min_route_spread_percentile: float
    min_route_spread_zscore: float
    max_route_spread_trend_pct: float
    max_adverse_funding_pct: float
    min_persistence_count: int


def passes_rules(row: dict, rules: RuleSet, require_paper_ready: bool) -> bool:
    if row.get("instrument_class") != "crypto":
        return False
    if not parse_bool(row.get("long_fillable")) or not parse_bool(row.get("short_fillable")):
        return False
    if require_paper_ready and not parse_bool(row.get("paper_ready")):
        return False

    persistence_count = parse_float(row.get("persistence_count"))
    if persistence_count is None or persistence_count < rules.min_persistence_count:
        return False

    validated_spread = parse_float(row.get("validated_spread_pct"))
    net_ex = parse_float(row.get("net_edge_ex_funding_pct"))
    net_inc = parse_float(row.get("net_edge_inc_funding_pct"))
    if validated_spread is None or net_ex is None or net_inc is None:
        return False

    if validated_spread < rules.min_validated_spread_pct:
        return False
    if net_ex < rules.min_net_spread_ex_funding_pct:
        return False
    if net_inc < rules.min_net_edge_inc_funding_pct:
        return False

    route_percentile = parse_float(row.get("route_spread_percentile"))
    route_zscore = parse_float(row.get("route_spread_zscore"))
    if route_percentile is None or route_percentile < rules.min_route_spread_percentile:
        return False
    if route_zscore is None or route_zscore < rules.min_route_spread_zscore:
        return False

    route_trend = parse_float(row.get("route_spread_trend_pct"))
    if route_trend is not None and route_trend > rules.max_route_spread_trend_pct:
        return False

    funding_benefit = parse_float(row.get("funding_benefit_pct"))
    if funding_benefit is not None and funding_benefit < rules.max_adverse_funding_pct:
        return False

    return True


def load_rows(path: Path, horizon: int) -> tuple[list[dict], list[str]]:
    pnl_field = f"estimated_exit_net_pnl_pct_{horizon}m"
    compressed_field = f"spread_compressed_{horizon}m"
    with path.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        if pnl_field not in fieldnames:
            raise SystemExit(f"Missing {pnl_field}; rebuild labels with --horizons including {horizon}.")
        rows = [
            row
            for row in reader
            if parse_float(row.get(pnl_field)) is not None
            and row.get(compressed_field, "") != ""
        ]
    return rows, fieldnames


def summarise_rules(
    rows: list[dict],
    rules: RuleSet,
    horizon: int,
    max_window_minutes: int,
    require_paper_ready: bool,
) -> dict:
    pnl_field = f"estimated_exit_net_pnl_pct_{horizon}m"
    compressed_field = f"spread_compressed_{horizon}m"
    max_pnl_field = f"max_estimated_exit_net_pnl_pct_{max_window_minutes}m"
    adverse_field = f"max_adverse_widening_pct_points_{max_window_minutes}m"
    favourable_field = f"max_favourable_compression_pct_points_{max_window_minutes}m"

    selected = [row for row in rows if passes_rules(row, rules, require_paper_ready)]
    pnls = [parse_float(row.get(pnl_field)) for row in selected]
    pnls = [value for value in pnls if value is not None]
    wins = [value for value in pnls if value > 0]
    losses = [value for value in pnls if value < 0]
    max_path_pnls = [parse_float(row.get(max_pnl_field)) for row in selected]
    max_path_pnls = [value for value in max_path_pnls if value is not None]
    adverse = [parse_float(row.get(adverse_field)) for row in selected]
    adverse = [value for value in adverse if value is not None]
    favourable = [parse_float(row.get(favourable_field)) for row in selected]
    favourable = [value for value in favourable if value is not None]

    return {
        "min_validated_spread_pct": rules.min_validated_spread_pct,
        "min_net_spread_ex_funding_pct": rules.min_net_spread_ex_funding_pct,
        "min_net_edge_inc_funding_pct": rules.min_net_edge_inc_funding_pct,
        "min_route_spread_percentile": rules.min_route_spread_percentile,
        "min_route_spread_zscore": rules.min_route_spread_zscore,
        "max_route_spread_trend_pct": rules.max_route_spread_trend_pct,
        "max_adverse_funding_pct": rules.max_adverse_funding_pct,
        "min_persistence_count": rules.min_persistence_count,
        "require_paper_ready": require_paper_ready,
        "selected_rows": len(selected),
        "horizon_minutes": horizon,
        "win_rate_pct": (len(wins) / len(pnls) * 100) if pnls else "",
        "avg_exit_pnl_pct": (sum(pnls) / len(pnls)) if pnls else "",
        "total_exit_pnl_pct_units": sum(pnls) if pnls else "",
        "best_exit_pnl_pct": max(pnls) if pnls else "",
        "worst_exit_pnl_pct": min(pnls) if pnls else "",
        "avg_winner_pct": (sum(wins) / len(wins)) if wins else "",
        "avg_loser_pct": (sum(losses) / len(losses)) if losses else "",
        "avg_max_path_pnl_pct": (sum(max_path_pnls) / len(max_path_pnls)) if max_path_pnls else "",
        "avg_max_adverse_widening_pct_points": (sum(adverse) / len(adverse)) if adverse else "",
        "avg_max_favourable_compression_pct_points": (sum(favourable) / len(favourable)) if favourable else "",
        "compressed_rate_pct": (
            sum(1 for row in selected if parse_bool(row.get(compressed_field))) / len(selected) * 100
        ) if selected else "",
    }


def fmt(value) -> str:
    if value == "":
        return ""
    if isinstance(value, float):
        return f"{value:.10f}"
    return str(value)


def write_results(rows: list[dict], output_path: Path) -> None:
    fieldnames = [
        "min_validated_spread_pct",
        "min_net_spread_ex_funding_pct",
        "min_net_edge_inc_funding_pct",
        "min_route_spread_percentile",
        "min_route_spread_zscore",
        "max_route_spread_trend_pct",
        "max_adverse_funding_pct",
        "min_persistence_count",
        "require_paper_ready",
        "selected_rows",
        "horizon_minutes",
        "win_rate_pct",
        "avg_exit_pnl_pct",
        "total_exit_pnl_pct_units",
        "best_exit_pnl_pct",
        "worst_exit_pnl_pct",
        "avg_winner_pct",
        "avg_loser_pct",
        "avg_max_path_pnl_pct",
        "avg_max_adverse_widening_pct_points",
        "avg_max_favourable_compression_pct_points",
        "compressed_rate_pct",
    ]
    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: fmt(row.get(field, "")) for field in fieldnames})


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Offline sweep of spread-entry thresholds against labelled validated opportunities."
    )
    parser.add_argument("--input-date", help="Date in YYYYMMDD format. Defaults to latest labelled date.")
    parser.add_argument("--horizon", type=int, default=240, help="Exit horizon minutes to score.")
    parser.add_argument("--max-window-minutes", type=int, default=1440, help="Path window used by label file.")
    parser.add_argument("--min-validated-spread-grid", type=parse_grid, default=parse_grid("0.50,0.75,1.00"))
    parser.add_argument("--min-net-ex-grid", type=parse_grid, default=parse_grid("0.35,0.50,0.75"))
    parser.add_argument("--min-net-inc-grid", type=parse_grid, default=parse_grid("0.35,0.50,0.75"))
    parser.add_argument("--route-percentile-grid", type=parse_grid, default=parse_grid("0.55,0.65,0.75,0.85"))
    parser.add_argument("--route-zscore-grid", type=parse_grid, default=parse_grid("0.50,0.75,1.00,1.25"))
    parser.add_argument("--max-route-trend-grid", type=parse_grid, default=parse_grid("0.10,0.20,0.35"))
    parser.add_argument("--max-adverse-funding-grid", type=parse_grid, default=parse_grid("-0.05,-0.03,0.00"))
    parser.add_argument("--min-persistence-grid", type=parse_grid, default=parse_grid("1,2,3"))
    parser.add_argument("--min-selected-rows", type=int, default=25)
    parser.add_argument(
        "--require-paper-ready",
        action="store_true",
        help=(
            "Require scanner paper_ready=True. Off by default so sweeps can test "
            "alternative thresholds without baking in the current scanner readiness rule."
        ),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    input_date = args.input_date or latest_labelled_date()
    input_path = LABEL_INPUT_DIR / f"labelled_validated_opportunities_{input_date}.csv"
    if not input_path.exists():
        raise SystemExit(f"Labelled file not found: {input_path}")

    rows, _ = load_rows(input_path, args.horizon)
    results = []
    for values in product(
        args.min_validated_spread_grid,
        args.min_net_ex_grid,
        args.min_net_inc_grid,
        args.route_percentile_grid,
        args.route_zscore_grid,
        args.max_route_trend_grid,
        args.max_adverse_funding_grid,
        args.min_persistence_grid,
    ):
        rules = RuleSet(
            min_validated_spread_pct=values[0],
            min_net_spread_ex_funding_pct=values[1],
            min_net_edge_inc_funding_pct=values[2],
            min_route_spread_percentile=values[3],
            min_route_spread_zscore=values[4],
            max_route_spread_trend_pct=values[5],
            max_adverse_funding_pct=values[6],
            min_persistence_count=int(values[7]),
        )
        summary = summarise_rules(
            rows=rows,
            rules=rules,
            horizon=args.horizon,
            max_window_minutes=args.max_window_minutes,
            require_paper_ready=args.require_paper_ready,
        )
        if int(summary["selected_rows"]) >= args.min_selected_rows:
            results.append(summary)

    results.sort(
        key=lambda row: (
            float(row["avg_exit_pnl_pct"]) if row["avg_exit_pnl_pct"] != "" else -999,
            int(row["selected_rows"]),
        ),
        reverse=True,
    )

    output_path = SWEEP_OUTPUT_DIR / f"validated_entry_rule_sweep_{input_date}_{args.horizon}m.csv"
    write_results(results, output_path)

    print(f"Input file: {input_path}")
    print(f"Output file: {output_path}")
    print(f"Rows scored: {len(rows)}")
    print(f"Rule sets kept: {len(results)}")
    print(f"Horizon minutes: {args.horizon}")
    print("Top 10 rule sets by average exit PnL:")
    for row in results[:10]:
        print(
            "  "
            f"entries={row['selected_rows']} "
            f"avg_pnl={fmt(row['avg_exit_pnl_pct'])} "
            f"win_rate={fmt(row['win_rate_pct'])} "
            f"spread>={row['min_validated_spread_pct']} "
            f"net_ex>={row['min_net_spread_ex_funding_pct']} "
            f"net_inc>={row['min_net_edge_inc_funding_pct']} "
            f"pctile>={row['min_route_spread_percentile']} "
            f"z>={row['min_route_spread_zscore']} "
            f"trend<={row['max_route_spread_trend_pct']} "
            f"funding>={row['max_adverse_funding_pct']} "
            f"persist>={row['min_persistence_count']}"
        )


if __name__ == "__main__":
    main()
