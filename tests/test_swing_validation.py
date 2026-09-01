from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta

import pytest

from trading_agent.domain.models import DailyBar
from trading_agent.swing.backtest import run_backtest
from trading_agent.swing.models import AssetType, SwingConfig, TransactionCosts
from trading_agent.swing.validation import (
    STATUS_DATA_INSUFFICIENT,
    STATUS_OK,
    run_robustness_checks,
)


def _rising_bars(count: int = 100) -> list[DailyBar]:
    """虚构的匿名单调路径：只验证回放结构，不对应任何证券。"""

    origin = datetime(2025, 1, 1, tzinfo=UTC)
    bars: list[DailyBar] = []
    for index in range(count):
        close = 10.0 + index * 0.05
        previous_close = 10.0 + max(0, index - 1) * 0.05
        open_price = previous_close
        bars.append(
            DailyBar(
                trade_date=origin + timedelta(days=index),
                open_price=open_price,
                high_price=max(open_price, close) + 0.1,
                low_price=min(open_price, close) - 0.1,
                close_price=close,
                volume=100.0,
                turnover_amount=close * 100.0,
            )
        )
    return bars


@pytest.fixture
def validation_config() -> SwingConfig:
    return SwingConfig(
        price_position_window=10,
        trend_fast_window=3,
        trend_slow_window=5,
        rsi_window=5,
        atr_window=5,
        bollinger_window=10,
        relative_volume_window=5,
        min_history_bars=20,
        min_holding_bars=10,
        action_cooldown_bars=10,
    )


def test_checks_split_valid_curve_into_three_even_non_overlapping_phases(validation_config) -> None:
    summary = run_robustness_checks(
        _rising_bars(),
        asset_type=AssetType.ETF,
        config=validation_config,
        costs=TransactionCosts(commission_bps=3.0, slippage_bps=5.0),
    )

    assert summary.status == STATUS_OK
    assert summary.baseline.status == STATUS_OK
    assert len(summary.phase_scenarios) == 3
    counts = [item.observation_count for item in summary.phase_scenarios]
    assert all(count is not None and count >= 20 for count in counts)
    assert max(counts) - min(counts) <= 1  # type: ignore[arg-type]
    assert sum(counts) == summary.baseline.observation_count
    assert summary.phase_scenarios[0].end_date < summary.phase_scenarios[1].start_date
    assert summary.phase_scenarios[1].end_date < summary.phase_scenarios[2].start_date
    for item in summary.phase_scenarios:
        assert item.dynamic_return is not None
        assert item.buy_hold_return is not None
        assert item.static_return is not None
        assert item.excess_vs_buy_hold is not None
        assert item.excess_vs_static is not None
        assert item.dynamic_max_drawdown is not None
        assert item.trade_count is not None


def test_fixed_parameter_and_cost_scenarios_are_explicit(validation_config) -> None:
    summary = run_robustness_checks(
        _rising_bars(),
        config=validation_config,
        costs=TransactionCosts(commission_bps=4.0, slippage_bps=6.0, sell_tax_bps=2.0),
    )

    assert [item.scenario for item in summary.scenarios] == [
        "phase_1",
        "phase_2",
        "phase_3",
        "parameter_tighten_20pct",
        "parameter_loosen_20pct",
        "cost_2x",
        "cost_3x",
    ]
    assert [item.parameter_profile for item in summary.parameter_scenarios] == [
        "tighten_20pct",
        "loosen_20pct",
    ]
    assert [item.cost_multiplier for item in summary.cost_scenarios] == [2.0, 3.0]
    for item in summary.parameter_scenarios + summary.cost_scenarios:
        assert item.dynamic_return is not None
        assert item.buy_hold_return is not None
        assert item.static_return is not None
        assert item.excess_vs_buy_hold is not None
        assert item.excess_vs_static is not None
        assert item.dynamic_max_drawdown is not None
        assert item.trade_count is not None


def test_direction_consistency_uses_only_four_variants_and_10bp_tolerance(validation_config) -> None:
    summary = run_robustness_checks(
        _rising_bars(),
        config=validation_config,
        costs=TransactionCosts(commission_bps=4.0, slippage_bps=6.0),
    )

    # 单调上涨路径不会触发高位减仓，四个变体与基础回放都相对全程持有
    # 方向一致（核心仓的现金比例使超额方向为负）。
    assert summary.direction_total == 4
    assert summary.direction_consistent_count == 4
    assert summary.direction_tolerance == pytest.approx(0.001)


def test_insufficient_curve_has_no_performance_numbers(validation_config) -> None:
    summary = run_robustness_checks(
        _rising_bars(50),
        config=validation_config,
        costs=TransactionCosts(),
    )

    assert summary.status == STATUS_DATA_INSUFFICIENT
    assert summary.baseline.status == STATUS_DATA_INSUFFICIENT
    assert summary.baseline.dynamic_return is None
    assert summary.baseline.buy_hold_return is None
    assert summary.direction_consistent_count is None
    assert summary.direction_total is None
    assert len(summary.scenarios) == 7
    for item in summary.scenarios:
        assert item.status == STATUS_DATA_INSUFFICIENT
        assert item.dynamic_return is None
        assert item.buy_hold_return is None
        assert item.static_return is None
        assert item.dynamic_max_drawdown is None
        assert item.trade_count is None


def test_precomputed_baseline_avoids_duplicate_base_run_and_does_not_mutate_config(
    validation_config, monkeypatch
) -> None:
    bars = _rising_bars()
    costs = TransactionCosts(commission_bps=4.0, slippage_bps=6.0, sell_tax_bps=2.0)
    baseline = run_backtest(
        bars,
        asset_type=AssetType.STOCK,
        config=validation_config,
        costs=costs,
    )
    before = asdict(validation_config)
    import trading_agent.swing.validation as validation_module

    original = validation_module.run_backtest
    calls: list[tuple[AssetType | str, TransactionCosts, SwingConfig]] = []

    def counted_run(*args: object, **kwargs: object):
        calls.append((kwargs["asset_type"], kwargs["costs"], kwargs["config"]))  # type: ignore[arg-type]
        return original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(validation_module, "run_backtest", counted_run)
    summary = run_robustness_checks(
        bars,
        asset_type=AssetType.STOCK,
        config=validation_config,
        costs=costs,
        base_result=baseline,
    )

    assert summary.status == STATUS_OK
    assert len(calls) == 4  # 2 parameter variants + 2 cost variants; baseline was reused.
    assert all(call[0] is AssetType.STOCK for call in calls)
    assert [call[1].commission_bps for call in calls[-2:]] == [8.0, 12.0]
    assert asdict(validation_config) == before


def test_summary_serializes_to_anonymous_json(validation_config) -> None:
    summary = run_robustness_checks(
        _rising_bars(),
        config=validation_config,
        costs=TransactionCosts(commission_bps=4.0, slippage_bps=6.0),
    )

    payload = summary.as_dict()
    serialized = json.dumps(payload, ensure_ascii=False)
    assert payload["interpretation"] == "sensitivity_check_not_strategy_proof"
    assert payload["direction_total"] == 4
    assert "code" not in serialized
    assert "name" not in serialized
