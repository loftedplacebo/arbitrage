from decimal import Decimal

from funding_lock_research.run_funding_lock_research import (
    build_event_rows,
    build_score_rows,
    estimate_rows_from_snapshot,
)


def test_estimate_rows_use_next_timestamp_for_next_rate():
    row = {
        "exchange": "OKX",
        "symbol": "BTC-USDT-SWAP",
        "observed_at_utc": "2026-01-01T00:00:00+00:00",
        "current_funding_rate": "0.0001",
        "next_funding_rate": "-0.0002",
        "settlement_time_ms": "1767225600000",
        "next_settlement_time_ms": "1767240000000",
    }

    estimates = estimate_rows_from_snapshot(row)

    current = [item for item in estimates if item["rate_field"] == "current_funding_rate"][0]
    next_rate = [item for item in estimates if item["rate_field"] == "next_funding_rate"][0]
    assert current["settlement_time_ms"] == 1767225600000
    assert next_rate["settlement_time_ms"] == 1767240000000


def test_event_and_score_rows_flag_reversal():
    comparisons = [
        {
            "exchange": "MEXC",
            "symbol": "ABC_USDT",
            "settlement_time_ms": "1767225600000",
            "settlement_time_utc": "2026-01-01T00:00:00+00:00",
            "rate_field": "current_funding_rate",
            "observed_at_utc": "2025-12-31T23:30:00+00:00",
            "bucket": "lte_30m",
            "estimated_rate": "0.0003",
            "settled_rate": "-0.0001",
            "abs_error": "0.0004",
            "matched_within_tolerance": False,
            "sign_flipped": True,
            "positive_receiver_reversed": True,
            "negative_receiver_reversed": False,
        }
    ]

    events = build_event_rows(comparisons, Decimal("0.00000001"))
    scores = build_score_rows(comparisons)

    assert events[0]["status"] == "fixed_but_last_did_not_match"
    assert scores[0]["sign_flip_pct"] == 100.0
    assert scores[0]["positive_receiver_reversal_pct"] == 100.0


if __name__ == "__main__":
    test_estimate_rows_use_next_timestamp_for_next_rate()
    test_event_and_score_rows_flag_reversal()
    print("funding lock research tests passed")
