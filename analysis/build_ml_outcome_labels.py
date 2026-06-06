from __future__ import annotations

import argparse
import bisect
import csv
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
ML_OBSERVATION_DIR = REPO_ROOT / "data" / "ml" / "fast_spread_observations"
ML_LABEL_OUTPUT_DIR = REPO_ROOT / "data" / "ml" / "labelled_observations"
ML_LABEL_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_HORIZONS = [1, 3, 5, 15, 30]


def parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def parse_horizons(value: str) -> list[int]:
    horizons = []
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        horizons.append(int(item))
    if not horizons:
        raise argparse.ArgumentTypeError("At least one horizon is required.")
    return sorted(set(horizons))


def latest_observation_date() -> str:
    files = sorted(ML_OBSERVATION_DIR.glob("fast_spread_observations_*.csv"))
    if not files:
        raise SystemExit(f"No observation files found in {ML_OBSERVATION_DIR}")
    return files[-1].stem.removeprefix("fast_spread_observations_")


def observation_key(row: dict) -> str:
    return "|".join([
        row.get("symbol", ""),
        row.get("long_exchange", ""),
        row.get("short_exchange", ""),
        row.get("direction", ""),
    ])


def load_observations(path: Path) -> tuple[list[dict], list[str]]:
    with path.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        rows = []
        for row in reader:
            try:
                row["_timestamp"] = parse_timestamp(row["timestamp_utc"])
                row["_fast_spread_pct"] = float(row["fast_spread_pct"])
            except (KeyError, TypeError, ValueError):
                continue
            rows.append(row)
    return rows, fieldnames


def fmt_float(value: float | None) -> str:
    return "" if value is None else f"{value:.10f}"


def fmt_bool(value: bool | None) -> str:
    return "" if value is None else str(value)


def label_group(rows: list[dict], horizons: list[int]) -> int:
    rows.sort(key=lambda row: row["_timestamp"])
    timestamps = [row["_timestamp"] for row in rows]
    spreads = [row["_fast_spread_pct"] for row in rows]
    labelled_rows = 0

    for idx, row in enumerate(rows):
        current_timestamp = row["_timestamp"]
        current_spread = row["_fast_spread_pct"]
        row_had_future = False

        for horizon in horizons:
            target_timestamp = current_timestamp + timedelta(minutes=horizon)
            future_idx = bisect.bisect_left(timestamps, target_timestamp)

            future_spread = None
            spread_change = None
            compressed = None
            if future_idx < len(rows):
                future_spread = spreads[future_idx]
                spread_change = future_spread - current_spread
                compressed = future_spread < current_spread
                row_had_future = True

            row[f"future_spread_pct_{horizon}m"] = fmt_float(future_spread)
            row[f"spread_change_pct_points_{horizon}m"] = fmt_float(spread_change)
            row[f"spread_compressed_{horizon}m"] = fmt_bool(compressed)

        window_end = current_timestamp + timedelta(minutes=30)
        window_start_idx = idx + 1
        window_end_idx = bisect.bisect_right(timestamps, window_end)
        future_window_spreads = spreads[window_start_idx:window_end_idx]

        if future_window_spreads:
            min_future_spread = min(future_window_spreads)
            max_future_spread = max(future_window_spreads)
            max_favourable_compression = current_spread - min_future_spread
            max_adverse_widening = max_future_spread - current_spread
            row_had_future = True
        else:
            min_future_spread = None
            max_future_spread = None
            max_favourable_compression = None
            max_adverse_widening = None

        row["min_future_spread_30m"] = fmt_float(min_future_spread)
        row["max_future_spread_30m"] = fmt_float(max_future_spread)
        row["max_favourable_compression_30m"] = fmt_float(max_favourable_compression)
        row["max_adverse_widening_30m"] = fmt_float(max_adverse_widening)

        if row_had_future:
            labelled_rows += 1

    return labelled_rows


def build_labels(rows: list[dict], horizons: list[int]) -> int:
    grouped = defaultdict(list)
    for row in rows:
        grouped[observation_key(row)].append(row)

    labelled_rows = 0
    for group_rows in grouped.values():
        labelled_rows += label_group(group_rows, horizons)

    rows.sort(key=lambda row: (row["_timestamp"], observation_key(row)))
    return labelled_rows


def output_fieldnames(input_fieldnames: list[str], horizons: list[int]) -> list[str]:
    fields = list(input_fieldnames)
    for horizon in horizons:
        fields.extend([
            f"future_spread_pct_{horizon}m",
            f"spread_change_pct_points_{horizon}m",
            f"spread_compressed_{horizon}m",
        ])
    fields.extend([
        "min_future_spread_30m",
        "max_future_spread_30m",
        "max_favourable_compression_30m",
        "max_adverse_widening_30m",
    ])
    return fields


def write_labelled_observations(
    rows: list[dict],
    fieldnames: list[str],
    output_path: Path,
) -> None:
    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def build_for_date(input_date: str, horizons: list[int]) -> dict:
    input_path = ML_OBSERVATION_DIR / f"fast_spread_observations_{input_date}.csv"
    if not input_path.exists():
        raise SystemExit(f"Observation file not found: {input_path}")

    rows, input_fieldnames = load_observations(input_path)
    labelled_rows = build_labels(rows, horizons)

    output_path = ML_LABEL_OUTPUT_DIR / f"labelled_fast_spread_observations_{input_date}.csv"
    write_labelled_observations(
        rows=rows,
        fieldnames=output_fieldnames(input_fieldnames, horizons),
        output_path=output_path,
    )

    return {
        "input_file": input_path,
        "output_file": output_path,
        "rows_read": len(rows),
        "rows_labelled": labelled_rows,
        "horizons": horizons,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build offline future-spread outcome labels from ML fast-spread observations."
    )
    parser.add_argument("--input-date", help="Observation date in YYYYMMDD format. Defaults to latest available.")
    parser.add_argument("--horizons", type=parse_horizons, default=DEFAULT_HORIZONS, help="Comma-separated minutes.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    input_date = args.input_date or latest_observation_date()
    summary = build_for_date(input_date, args.horizons)

    print(f"Input file: {summary['input_file']}")
    print(f"Output file: {summary['output_file']}")
    print(f"Rows read: {summary['rows_read']}")
    print(f"Rows labelled: {summary['rows_labelled']}")
    print(f"Horizons used: {','.join(str(item) for item in summary['horizons'])}")


if __name__ == "__main__":
    main()
