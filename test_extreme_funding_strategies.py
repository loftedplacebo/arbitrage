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
from mexc_extreme_funding.mexc_public_client import MexcPublicClient
from mexc_extreme_funding.models import FundingSnapshot as MexcSnapshot
from mexc_extreme_funding.paper_store import PaperStore as MexcStore
from mexc_extreme_funding.paper_strategy import run_paper_strategy_once as run_mexc
from mexc_extreme_funding.scanner import scan_once as scan_mexc
from core.models import OrderBook, OrderBookLevel


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

    def fetch_orderbooks(self, spot_symbol, perp_symbol, observed_at, limit=100):
        bids = [OrderBookLevel(price=99.95, quantity=1_000.0)]
        asks = [OrderBookLevel(price=100.05, quantity=1_000.0)]
        return (
            OrderBook("binance", "spot", spot_symbol, spot_symbol, bids, asks, observed_at),
            OrderBook("binance", "futures", perp_symbol, perp_symbol, bids, asks, observed_at),
        )


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
                opportunities_dir=root / "opportunities",
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
            self.assertEqual(result["opportunities"], 4)
            self.assertEqual(result["errors"], [])
            self.assertTrue((root / "latest_snapshots.csv").exists())
            opportunities = BinanceStore.read_rows(root / "latest_opportunities.csv")
            self.assertTrue(all(row["round_trip_fillable"] == "True" for row in opportunities))
            self.assertTrue(all(float(row["expected_edge_pct"]) > 0 for row in opportunities))
            comparisons = BinanceStore.read_rows(root / "settlement_comparisons.csv")
            self.assertEqual(comparisons[0]["actual_rate_pct"], "0.55")
            self.assertEqual(comparisons[0]["same_direction"], "True")

    def test_binance_holds_adverse_basis_and_aggregates_layers_before_funding(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = replace(
                BINANCE_DEFAULT,
                data_dir=root,
                snapshots_dir=root / "snapshots",
                opportunities_dir=root / "opportunities",
                paper_dir=root / "paper",
            )
            start = datetime(2026, 7, 14, 8, 0, tzinfo=timezone.utc)
            funding_time = start + timedelta(hours=4)
            client = FakeClient(settled_rate_pct=0.55)

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
            self.assertEqual(len(positions), 1)
            self.assertEqual(positions[0].notional_usd, 350.0)

            exit_time = start + timedelta(minutes=32)
            converged = snapshot(BinanceSnapshot, exit_time, funding_time, 0.55, 1.50, "BINANCE", "TESTUSDT")
            result = run_binance(config, client=client, snapshots=[converged], now=exit_time)
            self.assertEqual(result["exits"], 0)
            positions = BinanceStore(config).load_positions()
            self.assertEqual(positions[0].status, "OPEN")
            self.assertEqual(positions[0].notional_usd, 850.0)

            adverse_time = start + timedelta(minutes=62)
            adverse = snapshot(BinanceSnapshot, adverse_time, funding_time, 0.54, 3.50, "BINANCE", "TESTUSDT")
            result = run_binance(config, client=client, snapshots=[adverse], now=adverse_time)
            self.assertEqual(result["opened"], 1)
            self.assertEqual(result["exits"], 0)
            position = BinanceStore(config).load_positions()[0]
            self.assertEqual(position.notional_usd, 1_850.0)
            self.assertEqual(position.status, "OPEN")

    def test_binance_can_close_profitable_basis_before_funding(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = replace(
                BINANCE_DEFAULT,
                data_dir=root,
                snapshots_dir=root / "snapshots",
                opportunities_dir=root / "opportunities",
                paper_dir=root / "paper",
                layer_ladder_usd=(100.0,),
            )
            start = datetime(2026, 7, 14, 8, 0, tzinfo=timezone.utc)
            funding_time = start + timedelta(hours=4)
            client = FakeClient()
            first = snapshot(BinanceSnapshot, start, funding_time, 0.65, 2.0, "BINANCE", "TESTUSDT")
            run_binance(config, client=client, snapshots=[first], now=start)
            second_time = start + timedelta(minutes=1)
            second = snapshot(BinanceSnapshot, second_time, funding_time, 0.64, 2.0, "BINANCE", "TESTUSDT")
            run_binance(config, client=client, snapshots=[second], now=second_time)

            exit_time = start + timedelta(minutes=2)
            converged = snapshot(BinanceSnapshot, exit_time, funding_time, 0.63, 0.90, "BINANCE", "TESTUSDT")
            result = run_binance(config, client=client, snapshots=[converged], now=exit_time)
            self.assertEqual(result["exits"], 1)
            position = BinanceStore(config).load_positions()[0]
            self.assertEqual(position.status, "CLOSED")
            self.assertEqual(position.exit_reason, "prefunding_basis_take_profit")
            self.assertGreater(position.realised_pnl_usd, 0)
            self.assertEqual(position.funding_events_captured, 0)

    def test_binance_prefunding_basis_exit_uses_one_chunk_and_does_not_relayer(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = replace(
                BINANCE_DEFAULT,
                data_dir=root,
                snapshots_dir=root / "snapshots",
                opportunities_dir=root / "opportunities",
                paper_dir=root / "paper",
            )
            start = datetime(2026, 7, 14, 8, 0, tzinfo=timezone.utc)
            funding_time = start + timedelta(hours=4)
            client = FakeClient()
            first = snapshot(BinanceSnapshot, start, funding_time, 0.65, 2.0, "BINANCE", "TESTUSDT")
            run_binance(config, client=client, snapshots=[first], now=start)
            second_time = start + timedelta(minutes=1)
            second = snapshot(BinanceSnapshot, second_time, funding_time, 0.64, 2.0, "BINANCE", "TESTUSDT")
            run_binance(config, client=client, snapshots=[second], now=second_time)
            third_time = start + timedelta(minutes=2)
            third = snapshot(BinanceSnapshot, third_time, funding_time, 0.63, 2.0, "BINANCE", "TESTUSDT")
            run_binance(config, client=client, snapshots=[third], now=third_time)
            self.assertEqual(BinanceStore(config).load_positions()[0].notional_usd, 350.0)

            exit_time = start + timedelta(minutes=3)
            converged = snapshot(BinanceSnapshot, exit_time, funding_time, 0.62, 0.90, "BINANCE", "TESTUSDT")
            result = run_binance(config, client=client, snapshots=[converged], now=exit_time)
            position = BinanceStore(config).load_positions()[0]
            self.assertEqual(result["partial_exits"], 1, (result, position))
            self.assertEqual(result["opened"], 0)
            self.assertEqual(position.status, "OPEN")
            self.assertLess(position.notional_usd, 350.0)
            self.assertGreater(position.notional_usd, 0.0)
            self.assertEqual(position.exit_reason, "prefunding_basis_take_profit_partial")

    def test_binance_captures_funding_then_only_unwinds_a_profitable_chunk(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = replace(
                BINANCE_DEFAULT,
                data_dir=root,
                snapshots_dir=root / "snapshots",
                opportunities_dir=root / "opportunities",
                paper_dir=root / "paper",
            )
            start = datetime(2026, 7, 14, 8, 0, tzinfo=timezone.utc)
            funding_time = start + timedelta(minutes=20)
            client = FakeClient(settled_rate_pct=0.60)
            first = snapshot(BinanceSnapshot, start, funding_time, 0.65, 2.0, "BINANCE", "TESTUSDT")
            run_binance(config, client=client, snapshots=[first], now=start)
            second = snapshot(
                BinanceSnapshot, start + timedelta(minutes=1), funding_time,
                0.64, 2.0, "BINANCE", "TESTUSDT",
            )
            run_binance(config, client=client, snapshots=[second], now=start + timedelta(minutes=1))

            after = snapshot(
                BinanceSnapshot, funding_time + timedelta(minutes=2), funding_time,
                0.10, 1.0, "BINANCE", "TESTUSDT",
            )
            after.eligible = False
            after.reason = "too_close_to_funding"
            result = run_binance(config, client=client, snapshots=[after], now=funding_time + timedelta(minutes=2))
            self.assertEqual(result["funding_captures"], 1)
            self.assertEqual(result["exits"], 1)
            position = BinanceStore(config).load_positions()[0]
            self.assertEqual(position.status, "CLOSED")
            self.assertEqual(position.funding_events_captured, 1)
            self.assertNotEqual(position.exit_reason, "max_adverse_basis")

    def test_binance_holds_unprofitable_adverse_basis_after_funding(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = replace(
                BINANCE_DEFAULT,
                data_dir=root,
                snapshots_dir=root / "snapshots",
                opportunities_dir=root / "opportunities",
                paper_dir=root / "paper",
            )
            start = datetime(2026, 7, 14, 8, 0, tzinfo=timezone.utc)
            funding_time = start + timedelta(minutes=20)
            client = FakeClient(settled_rate_pct=0.60)
            first = snapshot(BinanceSnapshot, start, funding_time, 0.65, 2.0, "BINANCE", "TESTUSDT")
            run_binance(config, client=client, snapshots=[first], now=start)
            second_time = start + timedelta(minutes=1)
            second = snapshot(BinanceSnapshot, second_time, funding_time, 0.64, 2.0, "BINANCE", "TESTUSDT")
            run_binance(config, client=client, snapshots=[second], now=second_time)

            after_time = funding_time + timedelta(minutes=2)
            adverse = snapshot(BinanceSnapshot, after_time, funding_time, 0.10, 4.0, "BINANCE", "TESTUSDT")
            adverse.eligible = False
            adverse.reason = "too_close_to_funding"
            result = run_binance(config, client=client, snapshots=[adverse], now=after_time)
            self.assertEqual(result["funding_captures"], 1)
            self.assertEqual(result["exits"], 0)
            self.assertEqual(result["partial_exits"], 0)
            position = BinanceStore(config).load_positions()[0]
            self.assertEqual(position.status, "OPEN")
            self.assertEqual(position.exit_reason, "exit_wanted_no_profitable_chunk")

    def test_mexc_uses_actual_settlement_and_smaller_first_layer(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = replace(
                MEXC_DEFAULT,
                data_dir=root,
                snapshots_dir=root / "snapshots",
                opportunities_dir=root / "opportunities",
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
            self.assertEqual(result["exits"], 0)
            position = MexcStore(config).load_positions()[0]
            self.assertEqual(position.actual_funding_rate_pct, -0.60)
            self.assertEqual(position.exit_reason, "exit_wanted_no_profitable_chunk")
            self.assertEqual(position.status, "OPEN")
            self.assertEqual(client.history_calls, 1)

    def test_mexc_scanner_builds_independent_depth_priced_opportunities(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = replace(
                MEXC_DEFAULT,
                data_dir=root,
                snapshots_dir=root / "snapshots",
                opportunities_dir=root / "opportunities",
                paper_dir=root / "paper",
            )
            now = datetime.now(timezone.utc)
            item = snapshot(
                MexcSnapshot, now - timedelta(minutes=2), now - timedelta(minutes=1),
                -0.60, -1.0, "MEXC", "TEST_USDT",
            )
            result = scan_mexc(config, client=FakeScannerClient([item], settled_rate_pct=-0.58))
            self.assertEqual(result["opportunities"], 4)
            self.assertEqual(result["errors"], [])
            rows = MexcStore.read_rows(root / "latest_opportunities.csv")
            self.assertTrue(all(row["round_trip_fillable"] == "True" for row in rows))
            self.assertTrue(all(float(row["expected_edge_pct"]) > 0 for row in rows))

    def test_mexc_prefunding_basis_exit_is_chunked_and_does_not_relayer(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = replace(
                MEXC_DEFAULT,
                data_dir=root,
                snapshots_dir=root / "snapshots",
                opportunities_dir=root / "opportunities",
                paper_dir=root / "paper",
            )
            start = datetime(2026, 7, 14, 8, 0, tzinfo=timezone.utc)
            funding_time = start + timedelta(hours=4)
            client = FakeClient()
            first = snapshot(MexcSnapshot, start, funding_time, -0.65, -1.0, "MEXC", "TEST_USDT")
            run_mexc(config, client=client, snapshots=[first], now=start)
            second_time = start + timedelta(minutes=1)
            second = snapshot(MexcSnapshot, second_time, funding_time, -0.64, -1.0, "MEXC", "TEST_USDT")
            run_mexc(config, client=client, snapshots=[second], now=second_time)
            third_time = start + timedelta(minutes=2)
            third = snapshot(MexcSnapshot, third_time, funding_time, -0.63, -1.0, "MEXC", "TEST_USDT")
            run_mexc(config, client=client, snapshots=[third], now=third_time)
            self.assertEqual(MexcStore(config).load_positions()[0].notional_usd, 150.0)

            exit_time = start + timedelta(minutes=3)
            converged = snapshot(MexcSnapshot, exit_time, funding_time, -0.62, 0.20, "MEXC", "TEST_USDT")
            result = run_mexc(config, client=client, snapshots=[converged], now=exit_time)
            self.assertEqual(result["partial_exits"], 1)
            self.assertEqual(result["opened"], 0)
            position = MexcStore(config).load_positions()[0]
            self.assertEqual(position.status, "OPEN")
            self.assertLess(position.notional_usd, 150.0)
            self.assertEqual(position.exit_reason, "prefunding_basis_take_profit_partial")

    def test_mexc_adverse_basis_holds_and_adds_the_next_layer(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = replace(
                MEXC_DEFAULT,
                data_dir=root,
                snapshots_dir=root / "snapshots",
                opportunities_dir=root / "opportunities",
                paper_dir=root / "paper",
            )
            start = datetime(2026, 7, 14, 8, 0, tzinfo=timezone.utc)
            funding_time = start + timedelta(hours=4)
            client = FakeClient()
            first = snapshot(MexcSnapshot, start, funding_time, -0.65, -1.0, "MEXC", "TEST_USDT")
            run_mexc(config, client=client, snapshots=[first], now=start)
            second_time = start + timedelta(minutes=1)
            second = snapshot(MexcSnapshot, second_time, funding_time, -0.64, -1.0, "MEXC", "TEST_USDT")
            run_mexc(config, client=client, snapshots=[second], now=second_time)

            adverse_time = start + timedelta(minutes=2)
            adverse = snapshot(MexcSnapshot, adverse_time, funding_time, -0.63, -2.0, "MEXC", "TEST_USDT")
            result = run_mexc(config, client=client, snapshots=[adverse], now=adverse_time)
            self.assertEqual(result["opened"], 1)
            self.assertEqual(result["exits"], 0)
            position = MexcStore(config).load_positions()[0]
            self.assertEqual(position.notional_usd, 150.0)
            self.assertEqual(position.status, "OPEN")

    def test_mexc_legacy_position_is_not_layered_or_repriced(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = replace(
                MEXC_DEFAULT,
                data_dir=root,
                snapshots_dir=root / "snapshots",
                opportunities_dir=root / "opportunities",
                paper_dir=root / "paper",
            )
            start = datetime(2026, 7, 14, 8, 0, tzinfo=timezone.utc)
            funding_time = start + timedelta(hours=4)
            client = FakeClient()
            first = snapshot(MexcSnapshot, start, funding_time, -0.65, -1.0, "MEXC", "TEST_USDT")
            run_mexc(config, client=client, snapshots=[first], now=start)
            second_time = start + timedelta(minutes=1)
            second = snapshot(MexcSnapshot, second_time, funding_time, -0.64, -1.0, "MEXC", "TEST_USDT")
            run_mexc(config, client=client, snapshots=[second], now=second_time)
            store = MexcStore(config)
            position = store.load_positions()[0]
            position.spot_qty = 0.0
            position.perp_qty = 0.0
            store.write_positions([position])

            third_time = start + timedelta(minutes=2)
            third = snapshot(MexcSnapshot, third_time, funding_time, -0.63, -2.0, "MEXC", "TEST_USDT")
            result = run_mexc(config, client=client, snapshots=[third], now=third_time)
            self.assertEqual(result["opened"], 0)
            position = store.load_positions()[0]
            self.assertEqual(position.notional_usd, 50.0)
            self.assertEqual(position.exit_reason, "legacy_position_missing_execution_quantities")

    def test_mexc_contract_depth_is_converted_to_base_quantity(self):
        config = replace(MEXC_DEFAULT, request_sleep_seconds=0.0)
        client = MexcPublicClient(config)

        def fake_get(base_url, path, params=None):
            if path == "/api/v3/depth":
                return {"bids": [["99", "2"]], "asks": [["101", "3"]]}
            if path == "/api/v1/contract/detail":
                return {"success": True, "data": [{"symbol": "TEST_USDT", "contractSize": "0.001"}]}
            return {"success": True, "data": {"bids": [["100", "10", "1"]], "asks": [["102", "20", "1"]]}}

        client._get = fake_get
        observed = datetime(2026, 7, 14, 8, 0, tzinfo=timezone.utc)
        spot_book, perp_book = client.fetch_orderbooks("TESTUSDT", "TEST_USDT", observed)
        self.assertEqual(spot_book.bids[0].quantity, 2.0)
        self.assertAlmostEqual(perp_book.bids[0].quantity, 0.01)
        self.assertAlmostEqual(perp_book.asks[0].quantity, 0.02)

    def test_repeated_strategy_pass_does_not_count_same_snapshot_twice(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = replace(
                BINANCE_DEFAULT,
                data_dir=root,
                snapshots_dir=root / "snapshots",
                opportunities_dir=root / "opportunities",
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
