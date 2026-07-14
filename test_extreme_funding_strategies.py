from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from binance_extreme_funding.config import DEFAULT_CONFIG as BINANCE_DEFAULT
from binance_extreme_funding.models import FundingSnapshot as BinanceSnapshot
from binance_extreme_funding.paper_store import PaperStore as BinanceStore
from binance_extreme_funding.paper_strategy import run_paper_strategy_once as run_binance
from binance_extreme_funding.scanner import scan_once as scan_binance
from mexc_extreme_funding.config import DEFAULT_CONFIG as MEXC_DEFAULT
from mexc_extreme_funding.models import FundingSnapshot as MexcSnapshot
from mexc_extreme_funding.paper_store import PaperStore as MexcStore
from mexc_extreme_funding.paper_strategy import run_paper_strategy_once as run_mexc


class FakeClient:
    def __init__(self, settled_rate_pct=None):
        self.settled_rate_pct = settled_rate_pct
        self.history_calls = 0

    def fetch_settled_rate(self, symbol, funding_time):
        self.history_calls += 1
        return self.settled_rate_pct


class FakeScannerClient(FakeClient):
    def __init__(self, snapshots, settled_rate_pct):
        super().__init__(settled_rate_pct)
        self.snapshots = snapshots

    def fetch_snapshots(self, now):
        return self.snapshots


def snapshot(snapshot_type, observed, funding_time, rate_pct, basis_pct, exchange, symbol):
    spot_bid = 99.95
    spot_ask = 100.05
    if rate_pct > 0:
        perp_bid = spot_ask * (1 + basis_pct / 100)
        perp_ask = perp_bid * 1.0005
    else:
        perp_ask = spot_bid * (1 + basis_pct / 100)
        perp_bid = perp_ask * 0.9995
    return snapshot_type(
        observed_at_utc=observed,
        exchange=exchange,
        base="TEST",
        spot_symbol="TESTUSDT",
        perp_symbol=symbol,
        current_funding_rate_pct=rate_pct,
        predicted_funding_rate_pct=rate_pct,
        next_funding_time_utc=funding_time,
        minutes_to_funding=(funding_time - observed).total_seconds() / 60,
        funding_interval_hours=8.0,
        index_price=100.0,
        mark_price=102.0,
        mark_index_basis_pct=2.0,
        spot_bid=spot_bid,
        spot_ask=spot_ask,
        perp_bid=perp_bid,
        perp_ask=perp_ask,
        executable_basis_pct=basis_pct,
        eligible=True,
        reason="eligible",
    )


class ExtremeFundingStrategyTests(unittest.TestCase):
    def test_scanner_owns_snapshot_and_settlement_comparison_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = replace(
                BINANCE_DEFAULT,
                data_dir=root,
                snapshots_dir=root / "snapshots",
                paper_dir=root / "paper",
            )
            now = datetime.now(timezone.utc)
            item = snapshot(
                BinanceSnapshot,
                now - timedelta(minutes=2),
                now - timedelta(minutes=1),
                0.60,
                1.50,
                "BINANCE",
                "TESTUSDT",
            )
            client = FakeScannerClient([item], settled_rate_pct=0.55)
            result = scan_binance(config, client=client)

            self.assertEqual(result["snapshots"], 1)
            self.assertEqual(result["comparisons"], 1)
            self.assertTrue((root / "latest_snapshots.csv").exists())
            comparisons = BinanceStore.read_rows(root / "settlement_comparisons.csv")
            self.assertEqual(comparisons[0]["actual_rate_pct"], "0.55")
            self.assertEqual(comparisons[0]["same_direction"], "True")

    def test_binance_layers_over_window_and_exits_on_basis_profit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = replace(
                BINANCE_DEFAULT,
                data_dir=root,
                snapshots_dir=root / "snapshots",
                paper_dir=root / "paper",
            )
            start = datetime(2026, 7, 14, 8, 0, tzinfo=timezone.utc)
            funding_time = start + timedelta(hours=4)
            client = FakeClient()

            first = snapshot(BinanceSnapshot, start, funding_time, 0.60, 2.00, "BINANCE", "TESTUSDT")
            result = run_binance(config, client=client, snapshots=[first], now=start)
            self.assertEqual(result["opened"], 0)

            second_time = start + timedelta(minutes=1)
            second = snapshot(BinanceSnapshot, second_time, funding_time, 0.58, 2.00, "BINANCE", "TESTUSDT")
            result = run_binance(config, client=client, snapshots=[second], now=second_time)
            self.assertEqual(result["opened"], 1)

            layer_time = start + timedelta(minutes=31)
            third = snapshot(BinanceSnapshot, layer_time, funding_time, 0.56, 2.00, "BINANCE", "TESTUSDT")
            result = run_binance(config, client=client, snapshots=[third], now=layer_time)
            self.assertEqual(result["opened"], 1)
            positions = BinanceStore(config).load_positions()
            self.assertEqual([position.notional_usd for position in positions], [100.0, 250.0])

            exit_time = start + timedelta(minutes=32)
            converged = snapshot(BinanceSnapshot, exit_time, funding_time, 0.55, 0.90, "BINANCE", "TESTUSDT")
            result = run_binance(config, client=client, snapshots=[converged], now=exit_time)
            self.assertEqual(result["exits"], 2)
            positions = BinanceStore(config).load_positions()
            self.assertTrue(all(position.status == "CLOSED" for position in positions))
            self.assertTrue(all(position.exit_reason == "basis_take_profit" for position in positions))
            self.assertTrue(all(position.realised_pnl_usd > 0 for position in positions))

    def test_mexc_uses_actual_settlement_and_smaller_first_layer(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = replace(
                MEXC_DEFAULT,
                data_dir=root,
                snapshots_dir=root / "snapshots",
                paper_dir=root / "paper",
            )
            start = datetime(2026, 7, 14, 8, 0, tzinfo=timezone.utc)
            funding_time = start + timedelta(minutes=20)
            client = FakeClient(settled_rate_pct=-0.60)
            first = snapshot(MexcSnapshot, start, funding_time, -0.65, -1.00, "MEXC", "TEST_USDT")
            run_mexc(config, client=client, snapshots=[first], now=start)

            second_time = start + timedelta(minutes=1)
            second = snapshot(MexcSnapshot, second_time, funding_time, -0.63, -1.00, "MEXC", "TEST_USDT")
            result = run_mexc(config, client=client, snapshots=[second], now=second_time)
            self.assertEqual(result["opened"], 1)
            self.assertEqual(MexcStore(config).load_positions()[0].notional_usd, 50.0)

            settled_time = funding_time + timedelta(minutes=2)
            settled = snapshot(MexcSnapshot, settled_time, funding_time, -0.10, -1.00, "MEXC", "TEST_USDT")
            settled.eligible = False
            settled.reason = "too_close_to_funding"
            result = run_mexc(config, client=client, snapshots=[settled], now=settled_time)
            self.assertEqual(result["funding_captures"], 1)
            self.assertEqual(result["exits"], 1)
            position = MexcStore(config).load_positions()[0]
            self.assertEqual(position.actual_funding_rate_pct, -0.60)
            self.assertEqual(position.exit_reason, "funding_captured_profitable")
            self.assertGreater(position.realised_pnl_usd, 0)
            self.assertEqual(client.history_calls, 1)

    def test_repeated_strategy_pass_does_not_count_same_snapshot_twice(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = replace(
                BINANCE_DEFAULT,
                data_dir=root,
                snapshots_dir=root / "snapshots",
                paper_dir=root / "paper",
            )
            now = datetime(2026, 7, 14, 8, 0, tzinfo=timezone.utc)
            item = snapshot(BinanceSnapshot, now, now + timedelta(hours=2), 0.70, 1.00, "BINANCE", "TESTUSDT")
            client = FakeClient()
            run_binance(config, client=client, snapshots=[item], now=now)
            run_binance(config, client=client, snapshots=[item], now=now + timedelta(seconds=30))
            signal = next(iter(BinanceStore(config).load_signals().values()))
            self.assertEqual(int(signal["observations"]), 1)
            self.assertEqual(len(BinanceStore(config).load_positions()), 0)


if __name__ == "__main__":
    unittest.main()
