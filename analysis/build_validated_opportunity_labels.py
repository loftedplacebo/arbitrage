from __future__ import annotations

import argparse
import bisect
import csv
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
VALIDATED_INPUT_DIR = REPO_ROOT / "data" / "validated_futures_futures_snapshots"
LABEL_OUTPUT_DIR = REPO_ROOT / "data" / "ml" / "labelled_validated_opportunities"
LABEL_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_HORIZONS = [5, 15, 30, 60, 240, 1440]


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_float(value: str | None) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_horizons(value: str) -> list[int]:
    horizons = []
    for item in value.split(","):
        item = item.strip()
        if item:
            horizons.append(int(item))
    if not horizons:
        raise argparse.ArgumentTypeError("At least one horizon is required.")
    return sorted(set(horizons))


def latest_input_date() -> str:
    files = sorted(VALIDATED_INPUT_DIR.glob("validated_futures_futures_*.csv"))
    dated_files = [
        path
        for path in files
        if path.stem.removeprefix("validated_futures_futures_").isdigit()
    ]
    if not dated_files:
        raise SystemExit(f"No validated scanner files found in {VALIDATED_INPUT_DIR}")
    return dated_files[-1].stem.removeprefix("validated_futures_futures_")


def input_dates(start_date: str, lookahead_days: int) -> list[str]:
    start = datetime.strptime(start_date, "%Y%m%d").replace(tzinfo=timezone.utc)
    return [
        (start + timedelta(days=offset)).strftime("%Y%m%d")
        for offset in range(lookahead_days + 1)
    ]


def validated_key(row: dict) -> str:
    return "|".join([
        row.get("symbol", ""),
        row.get("long_exchange", ""),
        row.get("short_exchange", ""),
        row.get("direction", ""),
        str(int(float(row.get("notional_usdt") or 0))),
    ])


def load_rows(input_date: str, lookahead_days: int) -> tuple[list[dict], list[str]]:
    rows: list[dict] = []
    fieldnames: list[str] = []

    for date_value in input_dates(input_date, lookahead_days):
        path = VALIDATED_INPUT_DIR / f"validated_futures_futures_{date_value}.csv"
        if not path.exists():
            continue
        with path.open("r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            if not fieldnames:
                fieldnames = reader.fieldnames or []
            for row in reader:
                timestamp = parse_timestamp(row.get("timestamp_utc"))
                spread = parse_float(row.get("validated_spread_pct"))
                if timestamp is None or spread is None:
                    continue
                row["_timestamp"] = timestamp
                row["_validated_spread_pct"] = spread
                row["_key"] = validated_key(row)
                rows.append(row)

    return rows, fieldnames


def fmt_float(value: float | None) -> str:
    return "" if value is None else f"{value:.10f}"


def fmt_bool(value: bool | None) -> str:
    return "" if value is None else str(value)


def estimate_exit_pnl_pct(entry: dict, future: dict) -> float | None:
    notional = parse_float(entry.get("notional_usdt"))
    long_entry = parse_float(entry.get("long_avg_price"))
    short_entry = parse_float(entry.get("short_avg_price"))
    long_close = parse_float(future.get("long_close_avg_price"))
    short_close = parse_float(future.get("short_close_avg_price"))
    fees_pct = parse_float(entry.get("fees_pct")) or 0.0

    if notional is None or notional <= 0:
        return None
    if not all(value is not None and value > 0 for value in [long_entry, short_entry, long_close, short_close]):
        return None

    long_qty = notional / long_entry
    short_qty = notional / short_entry
    long_pnl = long_qty * (long_close - long_entry)
    short_pnl = short_qty * (short_entry - short_close)
    gross_pct = ((long_pnl + short_pnl) / notional) * 100

    # The scanner's fees_pct is the estimated round-trip cost used in net-edge
    # calculations. Subtract it here so labels resemble strategy economics.
    return gross_pct - fees_pct


def label_group(rows: list[dict], horizons: list[int], max_window_minutes: int) -> int:
    rows.sort(key=lambda row: row["_timestamp"])
    timestamps = [row["_timestamp"] for row in rows]
    spreads = [row["_validated_spread_pct"] for row in rows]
    labelled_rows = 0

    for idx, row in enumerate(rows):
        current_timestamp = row["_timestamp"]
        current_spread = row["_validated_spread_pct"]
        row_had_future = False

        for horizon in horizons:
            target_timestamp = current_timestamp + timedelta(minutes=horizon)
            future_idx = bisect.bisect_left(timestamps, target_timestamp)
            future = rows[future_idx] if future_idx < len(rows) else None

            future_spread = future["_validated_spread_pct"] if future else None
            spread_change = future_spread - current_spread if future_spread is not None else None
            compressed = future_spread < current_spread if future_spread is not None else None
            estimated_exit_pnl = estimate_exit_pnl_pct(row, future) if future else None

            if future is not None:
                row_had_future = True

            row[f"future_validated_spread_pct_{horizon}m"] = fmt_float(future_spread)
            row[f"spread_change_pct_points_{horizon}m"] = fmt_float(spread_change)
            row[f"spread_compressed_{horizon}m"] = fmt_bool(compressed)
            row[f"estimated_exit_net_pnl_pct_{horizon}m"] = fmt_float(estimated_exit_pnl)
            row[f"estimated_exit_profitable_{horizon}m"] = fmt_bool(
                estimated_exit_pnl is not None and estimated_exit_pnl > 0
            ) if future is not None else ""

        window_end = current_timestamp + timedelta(minutes=max_window_minutes)
        window_start_idx = idx + 1
        window_end_idx = bisect.bisect_right(timestamps, window_end)
        future_rows = rows[window_start_idx:window_end_idx]
        future_spreads = spreads[window_start_idx:window_end_idx]

        if future_spreads:
            min_spread = min(future_spreads)
            max_spread = max(future_spreads)
            max_favourable_compression = current_spread - min_spread
            max_adverse_widening = max_spread - current_spread

            future_pnls = [
                value
                for value in (estimate_exit_pnl_pct(row, future) for future in future_rows)
                if value is not None
            ]
            max_estimated_pnl = max(future_pnls) if future_pnls else None
            min_estimated_pnl = min(future_pnls) if future_pnls else None
            row_had_future = True
        else:
            min_spread = None
            max_spread = None
            max_favourable_compression = None
            max_adverse_widening = None
            max_estimated_pnl = None
            min_estimated_pnl = None

        suffix = f"{max_window_minutes}m"
        row[f"min_future_validated_spread_pct_{suffix}"] = fmt_float(min_spread)
        row[f"max_future_validated_spread_pct_{suffix}"] = fmt_float(max_spread)
        row[f"max_favourable_compression_pct_points_{suffix}"] = fmt_float(max_favourable_compression)
        row[f"max_adverse_widening_pct_points_{suffix}"] = fmt_float(max_adverse_widening)
        row[f"max_estimated_exit_net_pnl_pct_{suffix}"] = fmt_float(max_estimated_pnl)
        row[f"min_estimated_exit_net_pnl_pct_{suffix}"] = fmt_float(min_estimated_pnl)
        row[f"ever_estimated_profitable_{suffix}"] = fmt_bool(
            max_estimated_pnl is not None and max_estimated_pnl > 0
        ) if future_spreads else ""

        if row_had_future:
            labelled_rows += 1

    return labelled_rows


def build_labels(rows: list[dict], horizons: list[int], max_window_minutes: int) -> int:
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["_key"]].append(row)

    labelled_rows = 0
    for group_rows in grouped.values():
        labelled_rows += label_group(group_rows, horizons, max_window_minutes)

    rows.sort(key=lambda row: (row["_timestamp"], row["_key"]))
    return labelled_rows


def output_fieldnames(input_fieldnames: list[str], horizons: list[int], max_window_minutes: int) -> list[str]:
    fields = list(input_fieldnames)
    for horizon in horizons:
        fields.extend([
            f"future_validated_spread_pct_{horizon}m",
            f"spread_change_pct_points_{horizon}m",
            f"spread_compressed_{horizon}m",
            f"estimated_exit_net_pnl_pct_{horizon}m",
            f"estimated_exit_profitable_{horizon}m",
        ])

    suffix = f"{max_window_minutes}m"
    fields.extend([
        f"min_future_validated_spread_pct_{suffix}",
        f"max_future_validated_spread_pct_{suffix}",
        f"max_favourable_compression_pct_points_{suffix}",
        f"max_adverse_widening_pct_points_{suffix}",
        f"max_estimated_exit_net_pnl_pct_{suffix}",
        f"min_estimated_exit_net_pnl_pct_{suffix}",
        f"ever_estimated_profitable_{suffix}",
    ])
    return fields


def write_rows(rows: list[dict], fieldnames: list[str], output_path: Path) -> None:
    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def build_for_date(
    input_date: str,
    horizons: list[int],
    lookahead_days: int,
    max_window_minutes: int,
) -> dict:
    rows, input_fieldnames = load_rows(input_date, lookahead_days)
    labelled_rows = build_labels(rows, horizons, max_window_minutes)

    output_path = LABEL_OUTPUT_DIR / f"labelled_validated_opportunities_{input_date}.csv"
    write_rows(
        rows=rows,
        fieldnames=output_fieldnames(input_fieldnames, horizons, max_window_minutes),
        output_path=output_path,
    )

    return {
        "input_date": input_date,
        "output_file": output_path,
        "rows_read": len(rows),
        "rows_labelled": labelled_rows,
        "horizons": horizons,
        "lookahead_days": lookahead_days,
        "max_window_minutes": max_window_minutes,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build future outcome labels from deep-validated futures-futures opportunities."
    )
    parser.add_argument("--input-date", help="Date in YYYYMMDD format. Defaults to latest validated date.")
    parser.add_argument("--horizons", type=parse_horizons, default=DEFAULT_HORIZONS, help="Comma-separated minutes.")
    parser.add_argument("--lookahead-days", type=int, default=1, help="Extra daily files to load for future labels.")
    parser.add_argument("--max-window-minutes", type=int, default=1440, help="Window used for best/worst path labels.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    input_date = args.input_date or latest_input_date()
    summary = build_for_date(
        input_date=input_date,
        horizons=args.horizons,
        lookahead_days=args.lookahead_days,
        max_window_minutes=args.max_window_minutes,
    )

    print(f"Input date: {summary['input_date']}")
    print(f"Output file: {summary['output_file']}")
    print(f"Rows read: {summary['rows_read']}")
    print(f"Rows labelled: {summary['rows_labelled']}")
    print(f"Horizons used: {','.join(str(item) for item in summary['horizons'])}")
    print(f"Lookahead days: {summary['lookahead_days']}")
    print(f"Max window minutes: {summary['max_window_minutes']}")


if __name__ == "__main__":
    main()
