from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from itertools import pairwise

import pytest

from trading_agent.domain.models import DailyBar
from trading_agent.swing.backtest import run_backtest
from trading_agent.swing.models import AssetType, SwingConfig, SwingState, TransactionCosts


def _scenario_bars() -> list[DailyBar]:
    # 仅用于测试的虚构价格路径：先下跌后反弹，再冲高回落，并在末端再次
    # 走低。数值没有对应任何真实证券。
    closes = [
        10.0,
        10.0,
        10.0,
        10.0,
        10.0,
        10.0,
        10.0,
        10.0,
        10.0,
        10.0,
        10.0,
        10.0,
        10.0,
        10.0,
        10.0,
        9.0,
        8.0,
        7.0,
        7.4,
        7.8,
        8.5,
        9.5,
        10.5,
        11.5,
        12.5,
        13.0,
        13.1,
        13.0,
        12.7,
        12.2,
        11.5,
        10.5,
        9.5,
        8.5,
        8.0,
        8.4,
    ]
    result: list[DailyBar] = []
    origin = datetime(2025, 1, 1, tzinfo=UTC)
    for index, close in enumerate(closes):
        open_price = closes[index - 1] if index else close
        result.append(
            DailyBar(
                trade_date=origin + timedelta(days=index),
                open_price=open_price,
                high_price=max(open_price, close) + 0.2,
                low_price=min(open_price, close) - 0.2,
                close_price=close,
                volume=100.0,
                turnover_amount=close * 100.0,
            )
        )
    return result


@pytest.fixture
def backtest_config() -> SwingConfig:
    return SwingConfig(
        price_position_window=10,
        trend_fast_window=3,
        trend_slow_window=5,
        rsi_window=5,
        atr_window=5,
        bollinger_window=10,
        relative_volume_window=5,
        min_history_bars=15,
        min_holding_bars=10,
        action_cooldown_bars=10,
    )


def test_backtest_uses_next_open_and_keeps_core_weight_constant(backtest_config) -> None:
    bars = _scenario_bars()
    result = run_backtest(
        bars,
        asset_type=AssetType.ETF,
        config=backtest_config,
        costs=TransactionCosts(),
    )

    top_index = next(
        index for index, decision in enumerate(result.decisions) if decision.state is SwingState.TOP_CONFIRMED
    )
    first_complete = next(
        index
        for index, decision in enumerate(result.decisions)
        if decision.state is not SwingState.DATA_INSUFFICIENT
    )
    start_close = bars[first_complete].close_price
    event = result.trades[0]
    assert event.signal_date == bars[top_index].trade_date
    assert event.execution_date == bars[top_index + 1].trade_date
    assert event.execution_price == pytest.approx(bars[top_index + 1].open_price)
    # 信号在 top_index 收盘形成，当日净值仍未发生减仓；成交只影响下一根 K。
    signal_point = next(
        point for point in result.equity_curve if point.trade_date == bars[top_index].trade_date
    )
    execution_point = next(
        point for point in result.equity_curve if point.trade_date == bars[top_index + 1].trade_date
    )
    assert signal_point.core_tactical_value == pytest.approx(
        signal_point.buy_and_hold_value
    )
    # 交易后核心仓仍是初始 80%；只有机动 20% 以 t+1 开盘价转成现金。
    expected = (
        backtest_config.core_weight * bars[top_index + 1].close_price / start_close
        + backtest_config.tactical_weight * bars[top_index + 1].open_price / start_close
    )
    assert execution_point.core_tactical_value == pytest.approx(expected)


def test_backtest_costs_are_applied_and_metrics_are_comparable(backtest_config) -> None:
    bars = _scenario_bars()
    free = run_backtest(bars, config=backtest_config, costs=TransactionCosts())
    costly = run_backtest(
        bars,
        config=backtest_config,
        costs=TransactionCosts(commission_bps=10.0, slippage_bps=20.0, sell_tax_bps=5.0),
    )

    assert free.start_date == costly.start_date
    assert free.end_date == costly.end_date
    assert free.buy_and_hold.total_return == costly.buy_and_hold.total_return
    assert costly.core_tactical.total_return < free.core_tactical.total_return
    assert costly.core_tactical.trade_count == free.core_tactical.trade_count
    assert costly.core_tactical.turnover == pytest.approx(free.core_tactical.turnover)
    assert costly.trades[0].cost > 0


def test_backtest_excludes_warmup_from_curve_and_metrics(backtest_config) -> None:
    bars = _scenario_bars()
    result = run_backtest(bars, config=backtest_config, costs=TransactionCosts())

    first_complete = next(
        index
        for index, decision in enumerate(result.decisions)
        if decision.state is not SwingState.DATA_INSUFFICIENT
    )
    assert result.start_date == bars[first_complete].trade_date
    assert result.equity_curve[0].trade_date == bars[first_complete].trade_date
    assert len(result.equity_curve) == len(bars) - first_complete
    assert result.buy_and_hold.final_value == pytest.approx(
        bars[-1].close_price / bars[first_complete].close_price
    )


def test_static_core_cash_baseline_is_mathematically_constant_exposure(backtest_config) -> None:
    bars = _scenario_bars()
    result = run_backtest(bars, config=backtest_config, costs=TransactionCosts())
    start_close = bars[next(i for i, d in enumerate(result.decisions) if d.state is not SwingState.DATA_INSUFFICIENT)].close_price

    assert result.static_core_cash is not None
    assert result.static_core_cash.trade_count == 0
    assert result.static_core_cash.turnover == pytest.approx(0.0)
    assert result.static_core_cash.market_exposure == pytest.approx(backtest_config.core_weight)
    for bar, point in zip(bars[-len(result.equity_curve) :], result.equity_curve):
        expected = backtest_config.core_weight * bar.close_price / start_close + backtest_config.tactical_weight
        assert point.static_core_cash_value == pytest.approx(expected)
    assert result.equity_curve[0].static_core_cash_value == pytest.approx(1.0)


def test_metrics_report_252_day_annualization_and_daily_exposure(backtest_config) -> None:
    result = run_backtest(_scenario_bars(), config=backtest_config, costs=TransactionCosts())
    values = [point.buy_and_hold_value for point in result.equity_curve]
    periods = len(values) - 1
    expected_return = (values[-1] / values[0]) ** (252 / periods) - 1
    daily_returns = [current / previous - 1 for previous, current in pairwise(values)]
    average = sum(daily_returns) / len(daily_returns)
    expected_volatility = (
        sum((item - average) ** 2 for item in daily_returns) / len(daily_returns) * 252
    ) ** 0.5

    assert result.buy_and_hold.annualized_return == pytest.approx(expected_return)
    assert result.buy_and_hold.annualized_volatility == pytest.approx(expected_volatility)
    assert result.buy_and_hold.market_exposure == pytest.approx(1.0)
    assert result.static_core_cash is not None
    assert result.static_core_cash.market_exposure == pytest.approx(backtest_config.core_weight)
    assert backtest_config.core_weight <= result.core_tactical.market_exposure <= 1.0


def test_zero_liquidity_delays_pending_target_to_next_available_bar(backtest_config) -> None:
    bars = _scenario_bars()
    probe = run_backtest(bars, config=backtest_config, costs=TransactionCosts())
    top_index = next(
        index for index, decision in enumerate(probe.decisions) if decision.state is SwingState.TOP_CONFIRMED
    )
    bars[top_index + 1] = replace(bars[top_index + 1], volume=0.0, turnover_amount=0.0)

    result = run_backtest(bars, config=backtest_config, costs=TransactionCosts())

    assert result.deferred_count >= 1
    assert len(result.trades) == 1
    assert result.trades[0].signal_date == bars[top_index].trade_date
    assert result.trades[0].execution_date == bars[top_index + 2].trade_date


@pytest.mark.parametrize(
    "overrides",
    (
        {"low_rsi_threshold": -1.0},
        {"high_rsi_threshold": 101.0},
        {"low_rsi_threshold": 60.0, "high_rsi_threshold": 60.0},
        {"low_rsi_threshold": 70.0, "high_rsi_threshold": 60.0},
        {"reversal_rsi_tolerance": -0.1},
        {"low_drawdown_threshold": -0.05, "high_drawdown_threshold": -0.10},
        {"low_drawdown_threshold": -0.05, "high_drawdown_threshold": -0.05},
        {"high_drawdown_threshold": 0.1},
    ),
)
def test_swing_config_rejects_invalid_threshold_ordering(backtest_config, overrides) -> None:
    with pytest.raises(ValueError):
        replace(backtest_config, **overrides)


def test_minimum_holding_and_cooldown_block_frequent_changes(backtest_config) -> None:
    bars = _scenario_bars()
    blocked_by_holding = run_backtest(
        bars,
        config=replace(backtest_config, min_holding_bars=100, action_cooldown_bars=0),
        costs=TransactionCosts(),
    )
    blocked_by_cooldown = run_backtest(
        bars,
        config=replace(backtest_config, min_holding_bars=0, action_cooldown_bars=100),
        costs=TransactionCosts(),
    )

    assert blocked_by_holding.trades == ()
    assert len(blocked_by_cooldown.trades) == 1


def test_future_bar_perturbation_does_not_change_prior_backtest_decisions(backtest_config) -> None:
    bars = _scenario_bars()
    altered = list(bars)
    altered[-1] = DailyBar(
        trade_date=altered[-1].trade_date,
        open_price=40.0,
        high_price=45.0,
        low_price=35.0,
        close_price=42.0,
        volume=999.0,
        turnover_amount=9999.0,
    )
    first = run_backtest(bars, config=backtest_config, costs=TransactionCosts())
    second = run_backtest(altered, config=backtest_config, costs=TransactionCosts())

    assert first.decisions[:-1] == second.decisions[:-1]
    assert first.trades[:-1] == second.trades[:-1]
