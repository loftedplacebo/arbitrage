from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from statistics import median

from mexc_extreme_funding.config import MexcExtremeFundingConfig
from mexc_extreme_funding.models import iso, parse_float, utc_now


FIELDS = [
    "timestamp_utc", "base", "spot_symbol", "perp_symbol", "spot_mid", "perp_mid",
    "basis_pct", "funding_rate_pct", "minutes_to_funding",
]


@dataclass(frozen=True)
class BasisStats:
    observation_count: int
    mean_pct: float | None
    median_pct: float | None
    std_pct: float | None
    percentile: float | None
    trend_pct: float | None


def append_basis_observation(
    *, config: MexcExtremeFundingConfig, base: str, spot_symbol: str, perp_symbol: str,
    spot_mid: float, perp_mid: float, basis_pct: float, funding_rate_pct: float | None,
    minutes_to_funding: float | None,
) -> None:
    path = config.data_dir / "basis_history.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists() and path.stat().st_size > 0
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        if not exists:
            writer.writeheader()
        writer.writerow({
            "timestamp_utc": iso(utc_now()), "base": base, "spot_symbol": spot_symbol,
            "perp_symbol": perp_symbol, "spot_mid": spot_mid, "perp_mid": perp_mid,
            "basis_pct": basis_pct, "funding_rate_pct": funding_rate_pct,
            "minutes_to_funding": minutes_to_funding,
        })


def calculate_basis_stats(
    *, config: MexcExtremeFundingConfig, base: str, current_basis_pct: float,
) -> BasisStats:
    path = config.data_dir / "basis_history.csv"
    values: list[float] = []
    if path.exists():
        with path.open("r", newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                value = parse_float(row.get("basis_pct"))
                if row.get("base") == base and value is not None:
                    values.append(value)
    values = values[-config.basis_history_lookback:]
    if not values:
        return BasisStats(0, None, None, None, None, None)
    count = len(values)
    mean_pct = sum(values) / count
    variance = sum((value - mean_pct) ** 2 for value in values) / count
    percentile = sum(value <= current_basis_pct for value in values) / count * 100
    trend_pct = values[-1] - values[0] if count >= 2 else None
    return BasisStats(count, mean_pct, median(values), math.sqrt(variance), percentile, trend_pct)
