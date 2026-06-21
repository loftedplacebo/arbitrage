from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory

from strategy.config import StrategyConfig
from scanners.fast_futures_futures_scanner import calculate_route_stats, classify_instrument
from strategy.entry_rules import evaluate_entry, evaluate_funding_capture_ready, route_spread_quality_decision
from strategy.exit_rules import estimate_position_pnl, evaluate_exit
from strategy.models import Position, ValidatedOpportunity, format_datetime
from strategy.paper_execution import PaperExecutionEngine
from strategy.position_store import CsvPositionStore
from strategy.risk_rules import evaluate_entry_risk
from strategy.run_strategy_loop import choose_best_entry_rows, process_scan
from strategy.live_exit_watcher import LiveOrderBookCache, process_live_exit_updates
from core.symbols import standard_to_exchange_symbol


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
        "route_observation_count": 100,
        "route_spread_mean_pct": 0.3,
        "route_spread_median_pct": 0.25,
        "route_spread_min_pct": 0.05,
        "route_spread_max_pct": 1.2,
        "route_spread_std_pct": 0.2,
        "route_spread_zscore": 2.5,
        "route_spread_percentile": 0.95,
        "route_spread_trend_pct": 0.0,
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


def test_best_entry_row_prefers_paper_ready_slice():
    config = StrategyConfig(max_slice_notional_usd=5_000.0)
    rows = [
        make_opportunity(
            notional_usdt=2_500.0,
            validated_spread_pct=3.0,
            net_edge_ex_funding_pct=2.0,
            net_edge_inc_funding_pct=1.9,
            paper_ready=False,
        ),
        make_opportunity(
            notional_usdt=100.0,
            validated_spread_pct=2.5,
            net_edge_ex_funding_pct=1.5,
            net_edge_inc_funding_pct=1.4,
            paper_ready=True,
        ),
    ]

    selected = choose_best_entry_rows(rows, config)

    assert len(selected) == 1
    assert selected[0].notional_usdt == 100.0
    assert selected[0].paper_ready is True


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
    assert normal_decision.reason == "spread_entry_ok"
    assert normal_decision.desired_notional_usd == 100.0

    disabled_normal_decision = evaluate_entry(normal, {}, StrategyConfig(normal_entries_enabled=False), now=now)
    assert disabled_normal_decision.should_enter is False
    assert disabled_normal_decision.reason == "normal_entries_disabled"

    funding_capture = make_opportunity(
        timestamp_utc=now,
        validated_spread_pct=0.35,
        net_edge_ex_funding_pct=0.25,
        net_edge_inc_funding_pct=0.40,
        funding_benefit_pct=0.05,
        long_next_funding_time_utc=now + timedelta(minutes=30),
        short_next_funding_time_utc=now + timedelta(minutes=30),
    )
    capture_config = StrategyConfig(funding_capture_entries_enabled=True)
    capture_decision = evaluate_entry(funding_capture, {}, capture_config, now=now)
    assert capture_decision.should_enter is True
    assert capture_decision.reason == "funding_capture_entry_ok"

    spread_first_config = StrategyConfig()
    spread_first_decision = evaluate_entry(funding_capture, {}, spread_first_config, now=now)
    assert spread_first_decision.should_enter is False
    assert spread_first_decision.reason == "validated_spread_below_threshold"


def test_normal_entry_too_close_to_funding_rejected_without_benefit():
    now = datetime.now(timezone.utc)
    opportunity = make_opportunity(
        timestamp_utc=now,
        validated_spread_pct=0.80,
        net_edge_ex_funding_pct=0.55,
        net_edge_inc_funding_pct=0.55,
        funding_benefit_pct=0.01,
        long_next_funding_time_utc=now + timedelta(minutes=15),
        short_next_funding_time_utc=now + timedelta(minutes=15),
    )
    decision = evaluate_entry(opportunity, {}, StrategyConfig(normal_entries_enabled=True), now=now)
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
        long_next_funding_time_utc=now + timedelta(minutes=15),
        short_next_funding_time_utc=now + timedelta(minutes=15),
    )
    decision = evaluate_entry(opportunity, {}, StrategyConfig(normal_entries_enabled=True), now=now)
    assert decision.should_enter is True
    assert decision.reason == "spread_entry_funding_bonus_ok"


def test_route_spread_quality_required_for_default_entry():
    config = StrategyConfig()
    weak_route = make_opportunity(route_spread_percentile=0.49, route_spread_zscore=2.0)
    ok, reason = route_spread_quality_decision(weak_route, config)
    assert ok is False
    assert reason == "route_spread_percentile_below_threshold"

    strong_route = make_opportunity(route_spread_percentile=0.90, route_spread_zscore=1.5)
    ok, reason = route_spread_quality_decision(strong_route, config)
    assert ok is True
    assert reason == "route_spread_quality_ok"


def test_route_stats_calculate_percentile_zscore_and_trend():
    stats = calculate_route_stats([0.1, 0.2, 0.3, 0.4], 0.5)
    assert stats["route_observation_count"] == 4
    assert stats["route_spread_percentile"] == 1.0
    assert stats["route_spread_zscore"] > 1.0
    assert stats["route_spread_trend_pct"] > 0


def test_funding_capture_does_not_rescue_weak_spread_by_default():
    now = datetime.now(timezone.utc)
    opportunity = make_opportunity(
        timestamp_utc=now,
        validated_spread_pct=0.35,
        net_edge_ex_funding_pct=0.25,
        net_edge_inc_funding_pct=0.40,
        funding_benefit_pct=0.05,
        long_next_funding_time_utc=now + timedelta(minutes=30),
        short_next_funding_time_utc=now + timedelta(minutes=30),
    )
    decision = evaluate_entry(opportunity, {}, StrategyConfig(), now=now)
    assert decision.should_enter is False
    assert decision.reason == "validated_spread_below_threshold"


def test_round_trip_close_liquidity_is_required_for_entry():
    now = datetime.now(timezone.utc)
    not_fillable = make_opportunity(
        timestamp_utc=now,
        long_close_fillable=False,
    )
    decision = evaluate_entry(not_fillable, {}, StrategyConfig(), now=now)
    assert decision.should_enter is False
    assert decision.reason == "round_trip_close_not_fillable"

    missing_prices = make_opportunity(
        timestamp_utc=now,
        long_close_avg_price=None,
    )
    decision = evaluate_entry(missing_prices, {}, StrategyConfig(), now=now)
    assert decision.should_enter is False
    assert decision.reason == "round_trip_close_prices_missing"


def test_entry_selector_prefers_round_trip_100_dollar_tier():
    config = StrategyConfig(initial_entry_slice_notional_usd=100.0)
    rows = [
        make_opportunity(
            notional_usdt=100.0,
            net_edge_inc_funding_pct=0.40,
            long_close_fillable=True,
            short_close_fillable=True,
        ),
        make_opportunity(
            notional_usdt=500.0,
            net_edge_inc_funding_pct=1.50,
            long_close_fillable=False,
        ),
    ]
    selected = choose_best_entry_rows(rows, config)
    assert len(selected) == 1
    assert selected[0].notional_usdt == 100.0


def test_funding_capture_cannot_bypass_safety():
    now = datetime.now(timezone.utc)
    config = StrategyConfig(funding_capture_entries_enabled=True)
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
        StrategyConfig(max_daily_entries=0, funding_capture_entries_enabled=True),
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
    decision = evaluate_exit(
        position,
        opportunity,
        StrategyConfig(take_profit_pct=1.0, exit_on_negative_funding=True),
        now=now,
    )
    assert decision.should_exit is False
    assert decision.reason == "funding_negative_but_not_near_event"


def test_negative_funding_default_exit_disabled_holds():
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
    assert decision.should_exit is False
    assert decision.reason == "negative_funding_exit_disabled_hold"


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
    decision = evaluate_exit(position, opportunity, StrategyConfig(exit_on_negative_funding=True), now=now)
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
            exit_on_negative_funding=True,
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
            exit_on_negative_funding=True,
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
            exit_on_negative_funding=True,
        ),
        now=now,
    )
    assert decision.should_exit is False
    assert decision.reason == "hold_negative_funding_small_profit_buffer_ok"


def test_max_hold_requires_profit_by_default():
    now = datetime.now(timezone.utc)
    old_position = make_position(created_at=now - timedelta(hours=25), updated_at=now)
    losing = make_opportunity(
        timestamp_utc=now,
        long_close_avg_price=99.90,
        short_close_avg_price=101.0,
        funding_benefit_pct=0.01,
    )
    decision = evaluate_exit(old_position, losing, StrategyConfig(), now=now)
    assert decision.should_exit is False
    assert decision.reason == "max_hold_reached_unprofitable_hold"

    profitable = make_opportunity(
        timestamp_utc=now,
        long_close_avg_price=100.20,
        short_close_avg_price=101.0,
        funding_benefit_pct=0.01,
    )
    decision = evaluate_exit(
        old_position,
        profitable,
        StrategyConfig(estimated_exit_fee_pct=0.0),
        now=now,
    )
    assert decision.should_exit is True
    assert decision.reason == "max_hold_hours_reached"


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


def test_close_liquidity_warning_persistent_becomes_exit_only():
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
    assert decision.should_exit is False
    assert decision.reason == "close_liquidity_exit_only"


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


def test_exit_only_position_blocks_new_slice():
    opportunity = make_opportunity()
    existing = make_position(exit_only=True)
    decision = evaluate_entry_risk(
        opportunity=opportunity,
        open_positions={opportunity.position_key: existing},
        config=StrategyConfig(),
        desired_notional_usd=100.0,
    )
    assert decision.allowed is False
    assert decision.reason == "existing_position_exit_only"


def test_position_csv_without_partial_exit_fields_stays_compatible():
    row = make_position().to_csv_row()
    row.pop("exit_only", None)
    row.pop("partial_close_count", None)
    row.pop("realised_spread_pnl", None)
    loaded = Position.from_csv_row(row)
    assert loaded.exit_only is False
    assert loaded.partial_close_count == 0
    assert loaded.realised_spread_pnl == 0.0


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


def test_existing_position_negative_funding_blocks_new_slice():
    opportunity = make_opportunity(funding_benefit_pct=-0.01)
    existing = make_position()
    decision = evaluate_entry_risk(
        opportunity=opportunity,
        open_positions={opportunity.position_key: existing},
        config=StrategyConfig(),
        desired_notional_usd=100.0,
    )
    assert decision.allowed is False
    assert decision.reason == "existing_position_negative_funding_no_add"


def test_stock_like_symbols_are_not_crypto():
    for symbol in [
        "QCOMUSDT",
        "AMDUSDT",
        "SOXLUSDT",
        "ARMUSDT",
        "SKHYNIXUSDT",
        "SAMSUNGUSDT",
        "MSTRUSDT",
        "APLDUSDT",
        "RDDTUSDT",
        "OKLOUSDT",
        "QNTSTOCKUSDT",
        "SOXSUSDT",
        "SQQQUSDT",
        "ANTHROPICUSDT",
        "BPUSDT",
        "XOMUSDT",
        "NATGASUSDT",
    ]:
        assert classify_instrument(symbol) == "tokenised_stock_or_synthetic"


def test_hyperliquid_symbol_mapping():
    assert standard_to_exchange_symbol("BTCUSDT", "hyperliquid", "futures") == "BTC"


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


def test_profitable_partial_close_reduces_position_and_tracks_realised_pnl():
    with TemporaryDirectory() as tmp:
        config = StrategyConfig(data_dir=tmp, estimated_exit_fee_pct=0.0)
        store = CsvPositionStore(config)
        engine = PaperExecutionEngine(config, store)
        position = make_position(total_notional_usd=200.0)
        opportunity = make_opportunity(
            long_close_avg_price=100.5,
            short_close_avg_price=100.5,
            long_close_fillable=True,
            short_close_fillable=True,
        )

        closed, chunk_pnl = engine.close_position_chunk(
            position=position,
            opportunity=opportunity,
            notional_usd=100.0,
            reason="take_profit_reached",
        )
        assert closed is False
        assert chunk_pnl > 0
        assert position.total_notional_usd == 100.0
        assert position.exit_only is True
        assert position.partial_close_count == 1
        assert position.realised_spread_pnl == chunk_pnl
        fills = store.load_fills()
        assert fills[-1]["event_type"] == "PARTIAL_CLOSE"
        assert float(fills[-1]["remaining_notional_usd"]) == 100.0

        closed, final_chunk_pnl = engine.close_position_chunk(
            position=position,
            opportunity=opportunity,
            notional_usd=100.0,
            reason="take_profit_reached",
        )
        assert closed is True
        assert final_chunk_pnl > 0
        assert position.status == "CLOSED"
        assert position.total_notional_usd == 0.0
        fills = store.load_fills()
        assert fills[-1]["event_type"] == "CLOSE_POSITION"
        assert float(fills[-1]["position_realised_pnl_usd"]) > final_chunk_pnl


def test_strategy_loop_unwinds_exit_only_position_in_profitable_chunk():
    with TemporaryDirectory() as tmp:
        config = StrategyConfig(
            data_dir=tmp,
            max_daily_entries=0,
            estimated_exit_fee_pct=0.0,
        )
        store = CsvPositionStore(config)
        engine = PaperExecutionEngine(config, store)
        position = make_position(total_notional_usd=200.0, exit_only=True)
        positions = {position.position_id: position}
        opportunity = make_opportunity(
            long_close_avg_price=100.5,
            short_close_avg_price=100.5,
            long_close_fillable=True,
            short_close_fillable=True,
        )

        process_scan(
            scan_time=format_datetime(opportunity.timestamp_utc),
            scan_rows=[opportunity],
            source_file=store.data_dir / "test.csv",
            store=store,
            engine=engine,
            config=config,
            positions=positions,
        )
        assert positions[position.position_id].total_notional_usd == 100.0
        assert store.load_fills()[-1]["event_type"] == "PARTIAL_CLOSE"
        decisions = [
            row for row in store.decisions_path.read_text(encoding="utf-8").splitlines()
            if "partial_exit_executed" in row
        ]
        assert len(decisions) == 1
        assert "100.00000000" in decisions[0]


def test_adaptive_partial_exit_uses_largest_profitable_available_chunk():
    with TemporaryDirectory() as tmp:
        config = StrategyConfig(
            data_dir=tmp,
            max_daily_entries=0,
            estimated_exit_fee_pct=0.0,
            partial_exit_chunk_ladder_usd=(500.0, 100.0),
        )
        store = CsvPositionStore(config)
        engine = PaperExecutionEngine(config, store)
        position = make_position(total_notional_usd=1_000.0, exit_only=True)
        positions = {position.position_id: position}
        opportunity = make_opportunity(
            notional_usdt=500.0,
            long_close_avg_price=100.5,
            short_close_avg_price=100.5,
            long_close_fillable=True,
            short_close_fillable=True,
        )

        process_scan(
            scan_time=format_datetime(opportunity.timestamp_utc),
            scan_rows=[opportunity],
            source_file=store.data_dir / "test.csv",
            store=store,
            engine=engine,
            config=config,
            positions=positions,
        )
        assert positions[position.position_id].total_notional_usd == 500.0
        assert float(store.load_fills()[-1]["notional_usd"]) == 500.0


def test_adaptive_partial_exit_falls_back_to_smaller_profitable_chunk():
    with TemporaryDirectory() as tmp:
        config = StrategyConfig(
            data_dir=tmp,
            max_daily_entries=0,
            estimated_exit_fee_pct=0.0,
            partial_exit_chunk_ladder_usd=(500.0, 100.0),
        )
        store = CsvPositionStore(config)
        engine = PaperExecutionEngine(config, store)
        position = make_position(total_notional_usd=500.0, exit_only=True)
        positions = {position.position_id: position}
        unprofitable_500 = make_opportunity(
            notional_usdt=500.0,
            long_close_avg_price=99.5,
            short_close_avg_price=101.5,
        )
        profitable_100 = make_opportunity(
            notional_usdt=100.0,
            long_close_avg_price=100.5,
            short_close_avg_price=100.5,
        )

        process_scan(
            scan_time=format_datetime(profitable_100.timestamp_utc),
            scan_rows=[unprofitable_500, profitable_100],
            source_file=store.data_dir / "test.csv",
            store=store,
            engine=engine,
            config=config,
            positions=positions,
        )
        assert positions[position.position_id].total_notional_usd == 400.0
        assert float(store.load_fills()[-1]["notional_usd"]) == 100.0


def test_live_exit_watcher_unwinds_exit_only_position_from_book_events():
    with TemporaryDirectory() as tmp:
        config = StrategyConfig(
            data_dir=tmp,
            estimated_exit_fee_pct=0.0,
            partial_exit_chunk_ladder_usd=(500.0, 100.0),
        )
        store = CsvPositionStore(config)
        engine = PaperExecutionEngine(config, store)
        position = make_position(total_notional_usd=500.0, exit_only=True)
        positions = {position.position_id: position}
        cache = LiveOrderBookCache()
        timestamp = datetime.now(timezone.utc).isoformat()
        cache.update_payload({
            "exchange": "binance",
            "symbol": "IDUSDT",
            "market_type": "futures",
            "observed_at_utc": timestamp,
            "bids": [[100.5, 10.0]],
            "asks": [[100.6, 10.0]],
        })
        cache.update_payload({
            "exchange": "kucoin",
            "symbol": "IDUSDT",
            "market_type": "futures",
            "observed_at_utc": timestamp,
            "bids": [[100.4, 10.0]],
            "asks": [[100.5, 10.0]],
        })

        executed = process_live_exit_updates(
            positions=positions,
            cache=cache,
            store=store,
            engine=engine,
            config=config,
            changed_exchange="kucoin",
            changed_symbol="IDUSDT",
        )
        assert executed == 1
        assert position.position_id not in positions
        assert store.load_fills()[-1]["event_type"] == "CLOSE_POSITION"


def test_persistent_liquidity_warning_does_not_create_full_close():
    with TemporaryDirectory() as tmp:
        config = StrategyConfig(data_dir=tmp, max_daily_entries=0)
        store = CsvPositionStore(config)
        engine = PaperExecutionEngine(config, store)
        position = make_position(total_notional_usd=200.0, close_liquidity_warning_count=2)
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
        assert positions[position.position_id].exit_only is True
        assert positions[position.position_id].total_notional_usd == 200.0
        assert not any(
            row.get("event_type") == "CLOSE_POSITION"
            for row in store.load_fills()
        )


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
    test_best_entry_row_prefers_paper_ready_slice()
    test_missing_opportunity_waits_until_threshold()
    test_daily_risk_state_from_fills()
    test_funding_capture_ready_window()
    test_entry_normal_and_funding_capture_modes()
    test_normal_entry_too_close_to_funding_rejected_without_benefit()
    test_normal_entry_near_funding_allowed_when_favourable()
    test_route_spread_quality_required_for_default_entry()
    test_route_stats_calculate_percentile_zscore_and_trend()
    test_funding_capture_does_not_rescue_weak_spread_by_default()
    test_round_trip_close_liquidity_is_required_for_entry()
    test_entry_selector_prefers_round_trip_100_dollar_tier()
    test_funding_capture_cannot_bypass_safety()
    test_exit_holds_for_favourable_funding_but_stop_loss_overrides()
    test_remaining_edge_low_requires_profitable_exit_buffer()
    test_negative_funding_far_away_holds()
    test_negative_funding_near_event_losing_exits()
    test_materially_negative_funding_near_event_exits_even_when_profitable()
    test_negative_funding_near_event_small_profit_exits()
    test_slight_negative_funding_near_event_decent_profit_holds()
    test_negative_funding_default_exit_disabled_holds()
    test_max_hold_requires_profit_by_default()
    test_stop_loss_disabled_holds_spread_widening_loss()
    test_stop_loss_enabled_overrides_negative_funding_hold()
    test_close_liquidity_warning_holds_first_scan()
    test_close_liquidity_warning_persistent_becomes_exit_only()
    test_close_liquidity_warning_stop_loss_exits()
    test_existing_position_with_close_liquidity_warning_blocks_new_slice()
    test_exit_only_position_blocks_new_slice()
    test_position_csv_without_partial_exit_fields_stays_compatible()
    test_existing_losing_position_blocks_new_slice()
    test_existing_position_negative_funding_blocks_new_slice()
    test_stock_like_symbols_are_not_crypto()
    test_hyperliquid_symbol_mapping()
    test_default_strategy_blocks_known_synthetic_symbols()
    test_paper_execution_uses_opportunity_timestamp_for_fills()
    test_profitable_partial_close_reduces_position_and_tracks_realised_pnl()
    test_strategy_loop_unwinds_exit_only_position_in_profitable_chunk()
    test_adaptive_partial_exit_uses_largest_profitable_available_chunk()
    test_adaptive_partial_exit_falls_back_to_smaller_profitable_chunk()
    test_live_exit_watcher_unwinds_exit_only_position_from_book_events()
    test_persistent_liquidity_warning_does_not_create_full_close()
    test_strategy_loop_increments_and_resets_close_liquidity_warning_count()
    print("strategy engine tests passed")
