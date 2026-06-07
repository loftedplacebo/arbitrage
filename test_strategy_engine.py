from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory

from strategy.config import StrategyConfig
from scanners.fast_futures_futures_scanner import classify_instrument
from strategy.entry_rules import evaluate_entry, evaluate_funding_capture_ready
from strategy.exit_rules import estimate_position_pnl, evaluate_exit
from strategy.models import Position, ValidatedOpportunity, format_datetime
from strategy.paper_execution import PaperExecutionEngine
from strategy.position_store import CsvPositionStore
from strategy.risk_rules import evaluate_entry_risk
from strategy.run_strategy_loop import choose_best_entry_rows, process_scan


def make_opportunity(**overrides):
    now = datetime.now(timezone.utc)
    data = {
        "timestamp_utc": now,
        "symbol": "IDUSDT",
        "instrument_class": "crypto",
        "notional_usdt": 100.0,
        "long_exchange": "binance",
        "short_exchange": "kucoin",
        "direction": "long_binance_short_kucoin",
        "fast_spread_pct": 1.0,
        "fast_long_ask": 100.0,
        "fast_short_bid": 101.0,
        "long_avg_price": 100.0,
        "short_avg_price": 101.0,
        "long_close_avg_price": 100.5,
        "short_close_avg_price": 100.5,
        "validated_spread_pct": 1.0,
        "long_funding_pct": 0.0,
        "short_funding_pct": 0.0,
        "funding_benefit_pct": 0.0,
        "slippage_pct": 0.0,
        "fees_pct": 0.1,
        "net_edge_ex_funding_pct": 0.5,
        "net_edge_inc_funding_pct": 0.5,
        "classification": "EXCELLENT",
        "long_fillable": True,
        "short_fillable": True,
        "long_close_fillable": True,
        "short_close_fillable": True,
        "close_slippage_pct": 0.0,
        "persistence_count": 2,
        "persistent": True,
        "spread_ready": True,
        "funding_adjusted_ready": True,
        "paper_ready": True,
        "combined_volume_usdt": 1_000_000,
        "long_next_funding_time_utc": None,
        "short_next_funding_time_utc": None,
    }
    data.update(overrides)
    return ValidatedOpportunity(**data)


def make_position(**overrides):
    now = datetime.now(timezone.utc)
    data = {
        "position_id": "IDUSDT|binance|kucoin",
        "symbol": "IDUSDT",
        "long_exchange": "binance",
        "short_exchange": "kucoin",
        "total_notional_usd": 100.0,
        "slice_count": 1,
        "average_long_entry_price": 100.0,
        "average_short_entry_price": 101.0,
        "entry_spread_pct": 1.0,
        "current_spread_pct": 1.0,
        "entry_net_edge_pct": 0.5,
        "current_net_edge_pct": 0.5,
        "realised_funding_pnl": 0.0,
        "unrealised_spread_pnl": 0.0,
        "estimated_close_cost": 0.0,
        "estimated_net_pnl": 0.0,
        "created_at": now,
        "updated_at": now,
        "missing_scan_count": 0,
        "status": "OPEN",
    }
    data.update(overrides)
    return Position(**data)


def test_pnl_uses_close_side_prices():
    position = make_position()
    opportunity = make_opportunity(
        long_avg_price=200.0,
        short_avg_price=1.0,
        long_close_avg_price=100.5,
        short_close_avg_price=100.5,
    )
    pnl, _, _ = estimate_position_pnl(position, opportunity, StrategyConfig(estimated_exit_fee_pct=0.0))
    assert round(pnl, 6) == round(0.5 + (100 / 101) * 0.5, 6)


def test_one_entry_row_per_position_per_scan():
    config = StrategyConfig(max_slice_notional_usd=100.0)
    rows = [
        make_opportunity(notional_usdt=100.0, net_edge_inc_funding_pct=0.3),
        make_opportunity(notional_usdt=500.0, net_edge_inc_funding_pct=0.9),
        make_opportunity(symbol="LITEUSDT", notional_usdt=100.0, net_edge_inc_funding_pct=0.2),
    ]
    selected = choose_best_entry_rows(rows, config)
    assert len(selected) == 2
    assert sum(1 for row in selected if row.position_key == "IDUSDT|binance|kucoin") == 1
    assert next(row for row in selected if row.symbol == "IDUSDT").notional_usdt == 100.0


def test_missing_opportunity_waits_until_threshold():
    config = StrategyConfig(max_missing_scans_before_exit=3)
    position = make_position(missing_scan_count=2)
    assert evaluate_exit(position, None, config).should_exit is False

    position.missing_scan_count = 3
    decision = evaluate_exit(position, None, config)
    assert decision.should_exit is False
    assert decision.reason == "opportunity_missing_unpriced_hold"

    exit_config = StrategyConfig(max_missing_scans_before_exit=3, exit_on_missing_opportunity=True)
    decision = evaluate_exit(position, None, exit_config)
    assert decision.should_exit is True
    assert decision.reason == "opportunity_missing_too_long"


def test_daily_risk_state_from_fills():
    with TemporaryDirectory() as tmp:
        store = CsvPositionStore(StrategyConfig(data_dir=tmp))
        now = datetime.now(timezone.utc)
        store.append_fill(
            {
                "timestamp_utc": format_datetime(now),
                "event_type": "OPEN_SLICE",
                "realised_pnl_usd": "0",
            }
        )
        store.append_fill(
            {
                "timestamp_utc": format_datetime(now),
                "event_type": "CLOSE_POSITION",
                "realised_pnl_usd": "-1.25",
            }
        )
        store.append_fill(
            {
                "timestamp_utc": format_datetime(now),
                "event_type": "CLOSE_POSITION",
                "realised_pnl_usd": "-0.75",
            }
        )
        state = store.calculate_daily_risk_state(now=now)
        assert state["daily_entry_count"] == 1
        assert state["daily_realised_pnl_usd"] == -2.0
        assert state["consecutive_losses"] == 2


def test_funding_capture_ready_window():
    now = datetime.now(timezone.utc)
    config = StrategyConfig()
    opportunity = make_opportunity(
        funding_benefit_pct=0.05,
        long_next_funding_time_utc=now + timedelta(minutes=30),
        short_next_funding_time_utc=now + timedelta(minutes=30),
    )
    assert evaluate_funding_capture_ready(opportunity, config, now) is True

    assert evaluate_funding_capture_ready(
        make_opportunity(
            funding_benefit_pct=-0.01,
            long_next_funding_time_utc=now + timedelta(minutes=30),
            short_next_funding_time_utc=now + timedelta(minutes=30),
        ),
        config,
        now,
    ) is False
    assert evaluate_funding_capture_ready(
        make_opportunity(
            funding_benefit_pct=0.05,
            long_next_funding_time_utc=now + timedelta(minutes=120),
            short_next_funding_time_utc=now + timedelta(minutes=120),
        ),
        config,
        now,
    ) is False
    assert evaluate_funding_capture_ready(
        make_opportunity(
            funding_benefit_pct=0.05,
            long_next_funding_time_utc=now + timedelta(seconds=30),
            short_next_funding_time_utc=now + timedelta(seconds=30),
        ),
        config,
        now,
    ) is False
    assert evaluate_funding_capture_ready(
        make_opportunity(funding_benefit_pct=0.05),
        config,
        now,
    ) is False


def test_entry_normal_and_funding_capture_modes():
    now = datetime.now(timezone.utc)
    normal = make_opportunity(timestamp_utc=now)
    normal_decision = evaluate_entry(normal, {}, StrategyConfig(), now=now)
    assert normal_decision.should_enter is True
    assert normal_decision.reason == "entry_ok"

    funding_capture = make_opportunity(
        timestamp_utc=now,
        validated_spread_pct=0.60,
        net_edge_ex_funding_pct=0.25,
        net_edge_inc_funding_pct=0.40,
        funding_benefit_pct=0.05,
        long_next_funding_time_utc=now + timedelta(minutes=30),
        short_next_funding_time_utc=now + timedelta(minutes=30),
    )
    capture_config = StrategyConfig()
    capture_decision = evaluate_entry(funding_capture, {}, capture_config, now=now)
    assert capture_decision.should_enter is True
    assert capture_decision.reason == "funding_capture_entry_ok"


def test_normal_entry_too_close_to_funding_rejected_without_benefit():
    now = datetime.now(timezone.utc)
    opportunity = make_opportunity(
        timestamp_utc=now,
        validated_spread_pct=0.80,
        net_edge_ex_funding_pct=0.55,
        net_edge_inc_funding_pct=0.55,
        funding_benefit_pct=0.01,
        long_next_funding_time_utc=now + timedelta(minutes=30),
        short_next_funding_time_utc=now + timedelta(minutes=30),
    )
    decision = evaluate_entry(opportunity, {}, StrategyConfig(), now=now)
    assert decision.should_enter is False
    assert decision.reason == "normal_entry_too_close_to_funding"


def test_normal_entry_near_funding_allowed_when_favourable():
    now = datetime.now(timezone.utc)
    opportunity = make_opportunity(
        timestamp_utc=now,
        validated_spread_pct=0.80,
        net_edge_ex_funding_pct=0.55,
        net_edge_inc_funding_pct=0.55,
        funding_benefit_pct=0.05,
        long_next_funding_time_utc=now + timedelta(minutes=30),
        short_next_funding_time_utc=now + timedelta(minutes=30),
    )
    decision = evaluate_entry(opportunity, {}, StrategyConfig(), now=now)
    assert decision.should_enter is True
    assert decision.reason == "entry_ok"


def test_funding_capture_cannot_bypass_safety():
    now = datetime.now(timezone.utc)
    config = StrategyConfig()
    funding_fields = {
        "timestamp_utc": now,
        "net_edge_ex_funding_pct": 0.25,
        "net_edge_inc_funding_pct": 0.40,
        "funding_benefit_pct": 0.05,
        "long_next_funding_time_utc": now + timedelta(minutes=30),
        "short_next_funding_time_utc": now + timedelta(minutes=30),
    }
    assert evaluate_entry(make_opportunity(**funding_fields, paper_ready=False), {}, config, now=now).reason == "paper_ready_false"
    assert evaluate_entry(make_opportunity(**funding_fields, persistence_count=1), {}, config, now=now).reason == "persistence_below_threshold"
    assert evaluate_entry(
        make_opportunity(**funding_fields, instrument_class="tokenised_stock_or_synthetic"),
        {},
        config,
        now=now,
    ).reason == "instrument_class_rejected"
    stale_fields = {**funding_fields, "timestamp_utc": now - timedelta(minutes=10)}
    stale = make_opportunity(**stale_fields)
    assert evaluate_entry(stale, {}, config, now=now).reason == "stale_opportunity"
    blocked = evaluate_entry(
        make_opportunity(**funding_fields),
        {},
        StrategyConfig(max_daily_entries=0),
        now=now,
        daily_entry_count=0,
    )
    assert blocked.reason == "max_daily_entries_reached"


def test_exit_holds_for_favourable_funding_but_stop_loss_overrides():
    now = datetime.now(timezone.utc)
    config = StrategyConfig(min_remaining_edge_pct=0.03, stop_loss_enabled=True, stop_loss_pct=-0.30)
    position = make_position()
    opportunity = make_opportunity(
        timestamp_utc=now,
        long_close_avg_price=99.95,
        short_close_avg_price=101.0,
        net_edge_inc_funding_pct=0.01,
        funding_benefit_pct=0.05,
        long_next_funding_time_utc=now + timedelta(minutes=10),
        short_next_funding_time_utc=now + timedelta(minutes=10),
    )
    decision = evaluate_exit(position, opportunity, config, now=now)
    assert decision.should_exit is False
    assert decision.reason == "hold_for_favourable_funding"

    losing = make_opportunity(
        timestamp_utc=now,
        long_close_avg_price=90.0,
        short_close_avg_price=110.0,
        net_edge_inc_funding_pct=0.01,
        funding_benefit_pct=0.05,
        long_next_funding_time_utc=now + timedelta(minutes=10),
        short_next_funding_time_utc=now + timedelta(minutes=10),
    )
    stop_decision = evaluate_exit(position, losing, config, now=now)
    assert stop_decision.should_exit is True
    assert stop_decision.reason == "stop_loss_reached"


def test_remaining_edge_low_requires_profitable_exit_buffer():
    now = datetime.now(timezone.utc)
    position = make_position()
    config = StrategyConfig(min_remaining_edge_pct=0.03, min_profit_to_exit_remaining_edge_pct=0.05)
    not_profitable = make_opportunity(
        timestamp_utc=now,
        long_close_avg_price=100.05,
        short_close_avg_price=101.0,
        net_edge_inc_funding_pct=0.01,
    )
    decision = evaluate_exit(position, not_profitable, config, now=now)
    assert decision.should_exit is False
    assert decision.reason == "remaining_edge_low_but_not_profitable"

    profitable = make_opportunity(
        timestamp_utc=now,
        long_close_avg_price=100.25,
        short_close_avg_price=101.0,
        net_edge_inc_funding_pct=0.01,
    )
    decision = evaluate_exit(position, profitable, config, now=now)
    assert decision.should_exit is True
    assert decision.reason == "remaining_edge_too_low"


def test_negative_funding_far_away_holds():
    now = datetime.now(timezone.utc)
    position = make_position()
    opportunity = make_opportunity(
        long_close_avg_price=100.05,
        short_close_avg_price=101.0,
        funding_benefit_pct=-0.01,
        long_next_funding_time_utc=now + timedelta(minutes=120),
        short_next_funding_time_utc=now + timedelta(minutes=120),
    )
    decision = evaluate_exit(position, opportunity, StrategyConfig(take_profit_pct=1.0), now=now)
    assert decision.should_exit is False
    assert decision.reason == "funding_negative_but_not_near_event"


def test_negative_funding_near_event_losing_exits():
    now = datetime.now(timezone.utc)
    position = make_position()
    opportunity = make_opportunity(
        long_close_avg_price=99.99,
        short_close_avg_price=101.0,
        funding_benefit_pct=-0.01,
        long_next_funding_time_utc=now + timedelta(minutes=10),
        short_next_funding_time_utc=now + timedelta(minutes=10),
    )
    decision = evaluate_exit(position, opportunity, StrategyConfig(), now=now)
    assert decision.should_exit is True
    assert decision.reason == "negative_funding_near_event_losing"


def test_materially_negative_funding_near_event_exits_even_when_profitable():
    now = datetime.now(timezone.utc)
    position = make_position()
    opportunity = make_opportunity(
        long_close_avg_price=100.25,
        short_close_avg_price=101.0,
        funding_benefit_pct=-0.05,
        long_next_funding_time_utc=now + timedelta(minutes=10),
        short_next_funding_time_utc=now + timedelta(minutes=10),
    )
    decision = evaluate_exit(
        position,
        opportunity,
        StrategyConfig(
            max_negative_funding_tolerated_pct=-0.03,
            take_profit_pct=1.0,
        ),
        now=now,
    )
    assert decision.should_exit is True
    assert decision.reason == "negative_funding_near_event_material"


def test_negative_funding_near_event_small_profit_exits():
    now = datetime.now(timezone.utc)
    position = make_position()
    opportunity = make_opportunity(
        long_close_avg_price=100.04,
        short_close_avg_price=101.0,
        funding_benefit_pct=-0.01,
        long_next_funding_time_utc=now + timedelta(minutes=10),
        short_next_funding_time_utc=now + timedelta(minutes=10),
    )
    decision = evaluate_exit(
        position,
        opportunity,
        StrategyConfig(
            estimated_exit_fee_pct=0.0,
            min_profit_to_hold_negative_funding_pct=0.10,
            take_profit_pct=1.0,
        ),
        now=now,
    )
    assert decision.should_exit is True
    assert decision.reason == "negative_funding_near_event_profit_too_small"


def test_slight_negative_funding_near_event_decent_profit_holds():
    now = datetime.now(timezone.utc)
    position = make_position()
    opportunity = make_opportunity(
        long_close_avg_price=100.15,
        short_close_avg_price=101.0,
        funding_benefit_pct=-0.01,
        long_next_funding_time_utc=now + timedelta(minutes=10),
        short_next_funding_time_utc=now + timedelta(minutes=10),
    )
    decision = evaluate_exit(
        position,
        opportunity,
        StrategyConfig(
            estimated_exit_fee_pct=0.0,
            min_profit_to_hold_negative_funding_pct=0.10,
            max_negative_funding_tolerated_pct=-0.03,
            take_profit_pct=1.0,
        ),
        now=now,
    )
    assert decision.should_exit is False
    assert decision.reason == "hold_negative_funding_small_profit_buffer_ok"


def test_stop_loss_disabled_holds_spread_widening_loss():
    now = datetime.now(timezone.utc)
    position = make_position()
    opportunity = make_opportunity(
        long_close_avg_price=90.0,
        short_close_avg_price=110.0,
        funding_benefit_pct=0.01,
        long_next_funding_time_utc=now + timedelta(minutes=120),
        short_next_funding_time_utc=now + timedelta(minutes=120),
    )
    decision = evaluate_exit(
        position,
        opportunity,
        StrategyConfig(stop_loss_enabled=False, stop_loss_pct=-1.00),
        now=now,
    )
    assert decision.should_exit is False
    assert decision.reason == "hold"


def test_stop_loss_enabled_overrides_negative_funding_hold():
    now = datetime.now(timezone.utc)
    position = make_position()
    opportunity = make_opportunity(
        long_close_avg_price=90.0,
        short_close_avg_price=110.0,
        funding_benefit_pct=-0.01,
        long_next_funding_time_utc=now + timedelta(minutes=120),
        short_next_funding_time_utc=now + timedelta(minutes=120),
    )
    decision = evaluate_exit(
        position,
        opportunity,
        StrategyConfig(stop_loss_enabled=True, stop_loss_pct=-1.00),
        now=now,
    )
    assert decision.should_exit is True
    assert decision.reason == "stop_loss_reached"


def test_close_liquidity_warning_holds_first_scan():
    position = make_position(close_liquidity_warning_count=1)
    opportunity = make_opportunity(
        long_close_fillable=False,
        short_close_fillable=True,
        long_close_avg_price=100.0,
        short_close_avg_price=101.0,
    )
    decision = evaluate_exit(position, opportunity, StrategyConfig(), now=datetime.now(timezone.utc))
    assert decision.should_exit is False
    assert decision.reason == "close_liquidity_warning_hold"


def test_close_liquidity_warning_persistent_exits():
    position = make_position(close_liquidity_warning_count=3)
    opportunity = make_opportunity(
        long_close_fillable=False,
        short_close_fillable=True,
        long_close_avg_price=100.0,
        short_close_avg_price=101.0,
    )
    decision = evaluate_exit(
        position,
        opportunity,
        StrategyConfig(close_liquidity_max_warning_scans=3),
        now=datetime.now(timezone.utc),
    )
    assert decision.should_exit is True
    assert decision.reason == "close_liquidity_warning_persistent"


def test_close_liquidity_warning_stop_loss_exits():
    position = make_position(close_liquidity_warning_count=1)
    opportunity = make_opportunity(
        long_close_fillable=False,
        short_close_fillable=True,
        long_close_avg_price=90.0,
        short_close_avg_price=110.0,
    )
    decision = evaluate_exit(
        position,
        opportunity,
        StrategyConfig(stop_loss_enabled=True, stop_loss_pct=-1.00),
        now=datetime.now(timezone.utc),
    )
    assert decision.should_exit is True
    assert decision.reason == "close_liquidity_warning_stop_loss"


def test_existing_position_with_close_liquidity_warning_blocks_new_slice():
    opportunity = make_opportunity()
    existing = make_position(close_liquidity_warning_count=1)
    decision = evaluate_entry_risk(
        opportunity=opportunity,
        open_positions={opportunity.position_key: existing},
        config=StrategyConfig(),
        desired_notional_usd=100.0,
    )
    assert decision.allowed is False
    assert decision.reason == "existing_position_close_liquidity_warning"


def test_existing_losing_position_blocks_new_slice():
    opportunity = make_opportunity()
    existing = make_position(estimated_net_pnl=-0.31, total_notional_usd=100.0)
    decision = evaluate_entry_risk(
        opportunity=opportunity,
        open_positions={opportunity.position_key: existing},
        config=StrategyConfig(max_existing_position_loss_pct_for_add=-0.30),
        desired_notional_usd=100.0,
    )
    assert decision.allowed is False
    assert decision.reason == "existing_position_too_negative_to_add"


def test_stock_like_symbols_are_not_crypto():
    for symbol in [
        "QCOMUSDT",
        "AMDUSDT",
        "SOXLUSDT",
        "ARMUSDT",
        "APLDUSDT",
        "RDDTUSDT",
        "OKLOUSDT",
        "QNTSTOCKUSDT",
        "SOXSUSDT",
        "SQQQUSDT",
        "ANTHROPICUSDT",
        "BPUSDT",
    ]:
        assert classify_instrument(symbol) == "tokenised_stock_or_synthetic"


def test_default_strategy_blocks_known_synthetic_symbols():
    now = datetime.now(timezone.utc)
    opportunity = make_opportunity(symbol="APLDUSDT", timestamp_utc=now)
    decision = evaluate_entry(opportunity, {}, StrategyConfig(), now=now)
    assert decision.should_enter is False
    assert decision.reason == "symbol_blocked"


def test_paper_execution_uses_opportunity_timestamp_for_fills():
    with TemporaryDirectory() as tmp:
        config = StrategyConfig(data_dir=tmp)
        store = CsvPositionStore(config)
        engine = PaperExecutionEngine(config, store)
        timestamp = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        opportunity = make_opportunity(timestamp_utc=timestamp)
        positions = {}
        position = engine.open_or_add_slice(
            opportunity=opportunity,
            positions=positions,
            notional_usd=100.0,
            reason="entry_ok",
        )
        fills = store.load_fills()
        assert position.created_at == timestamp
        assert fills[0]["timestamp_utc"] == format_datetime(timestamp)


def test_strategy_loop_increments_and_resets_close_liquidity_warning_count():
    with TemporaryDirectory() as tmp:
        config = StrategyConfig(data_dir=tmp, max_daily_entries=0)
        store = CsvPositionStore(config)
        engine = PaperExecutionEngine(config, store)
        position = make_position()
        positions = {position.position_id: position}

        bad_close = make_opportunity(
            long_close_fillable=False,
            short_close_fillable=True,
            long_close_avg_price=100.0,
            short_close_avg_price=101.0,
        )
        process_scan(
            scan_time=format_datetime(bad_close.timestamp_utc),
            scan_rows=[bad_close],
            source_file=store.data_dir / "test.csv",
            store=store,
            engine=engine,
            config=config,
            positions=positions,
        )
        assert positions[position.position_id].close_liquidity_warning_count == 1

        good_close = make_opportunity(
            long_close_fillable=True,
            short_close_fillable=True,
            long_close_avg_price=100.0,
            short_close_avg_price=101.0,
        )
        process_scan(
            scan_time=format_datetime(good_close.timestamp_utc),
            scan_rows=[good_close],
            source_file=store.data_dir / "test.csv",
            store=store,
            engine=engine,
            config=config,
            positions=positions,
        )
        assert positions[position.position_id].close_liquidity_warning_count == 0


if __name__ == "__main__":
    test_pnl_uses_close_side_prices()
    test_one_entry_row_per_position_per_scan()
    test_missing_opportunity_waits_until_threshold()
    test_daily_risk_state_from_fills()
    test_funding_capture_ready_window()
    test_entry_normal_and_funding_capture_modes()
    test_normal_entry_too_close_to_funding_rejected_without_benefit()
    test_normal_entry_near_funding_allowed_when_favourable()
    test_funding_capture_cannot_bypass_safety()
    test_exit_holds_for_favourable_funding_but_stop_loss_overrides()
    test_remaining_edge_low_requires_profitable_exit_buffer()
    test_negative_funding_far_away_holds()
    test_negative_funding_near_event_losing_exits()
    test_materially_negative_funding_near_event_exits_even_when_profitable()
    test_negative_funding_near_event_small_profit_exits()
    test_slight_negative_funding_near_event_decent_profit_holds()
    test_stop_loss_disabled_holds_spread_widening_loss()
    test_stop_loss_enabled_overrides_negative_funding_hold()
    test_close_liquidity_warning_holds_first_scan()
    test_close_liquidity_warning_persistent_exits()
    test_close_liquidity_warning_stop_loss_exits()
    test_existing_position_with_close_liquidity_warning_blocks_new_slice()
    test_existing_losing_position_blocks_new_slice()
    test_stock_like_symbols_are_not_crypto()
    test_default_strategy_blocks_known_synthetic_symbols()
    test_paper_execution_uses_opportunity_timestamp_for_fills()
    test_strategy_loop_increments_and_resets_close_liquidity_warning_count()
    print("strategy engine tests passed")
