from __future__ import annotations

from dataclasses import astuple
from datetime import UTC, date, datetime, timedelta

import pytest

from trading_agent.domain.models import DailyBar
from trading_agent.swing.features import calculate_swing_features, prepare_bars
from trading_agent.swing.models import SwingConfig


def _bars(closes: list[float], *, start: datetime | None = None) -> list[DailyBar]:
    origin = start or datetime(2025, 1, 1, tzinfo=UTC)
    result: list[DailyBar] = []
    for index, close in enumerate(closes):
        previous = closes[index - 1] if index else close
        open_price = previous
        result.append(
            DailyBar(
                trade_date=origin + timedelta(days=index),
                open_price=open_price,
                high_price=max(open_price, close) + 0.2,
                low_price=min(open_price, close) - 0.2,
                close_price=close,
                volume=100.0 + index,
                turnover_amount=close * (100.0 + index),
            )
        )
    return result


@pytest.fixture
def feature_config() -> SwingConfig:
    return SwingConfig(
        price_position_window=20,
        trend_fast_window=5,
        trend_slow_window=10,
        rsi_window=5,
        atr_window=5,
        bollinger_window=10,
        relative_volume_window=5,
        min_history_bars=20,
    )


def test_features_are_past_only_when_future_bars_are_added_or_changed(feature_config) -> None:
    closes = [10.0 + index * 0.05 for index in range(25)]
    prefix = _bars(closes)
    future_a = _bars(closes + [11.5, 11.7, 11.8])
    future_b = _bars(closes + [4.0, 3.5, 8.0])

    before = calculate_swing_features(prefix, config=feature_config)
    after_a = calculate_swing_features(future_a, config=feature_config)[: len(prefix)]
    after_b = calculate_swing_features(future_b, config=feature_config)[: len(prefix)]

    assert tuple(astuple(item) for item in before) == tuple(astuple(item) for item in after_a)
    assert tuple(astuple(item) for item in before) == tuple(astuple(item) for item in after_b)


def test_feature_windows_and_relative_volume_use_explicit_past_lengths(feature_config) -> None:
    values = calculate_swing_features(_bars([10.0] * 20 + [12.0]), config=feature_config)

    assert values[19].price_position is not None
    assert values[19].rsi is not None
    assert values[19].relative_volume is not None
    # 当前成交量不进入自己的相对量基准；第 21 根使用前 5 根的平均量。
    assert values[20].relative_volume == pytest.approx(120.0 / 117.0)
    assert values[0].price_position is None
    assert values[19].bars_available == feature_config.min_history_bars


def test_prepare_bars_excludes_same_day_bar_before_close() -> None:
    bars = _bars([10.0, 10.2], start=datetime(2025, 1, 2, tzinfo=UTC))

    assert len(prepare_bars(bars, as_of=datetime(2025, 1, 2, 14, 59, tzinfo=UTC))) == 0
    assert len(prepare_bars(bars, as_of=datetime(2025, 1, 2, 15, 0, tzinfo=UTC))) == 1
    assert len(prepare_bars(bars, as_of=date(2025, 1, 2))) == 1


def test_prepare_bars_rejects_invalid_or_duplicate_daily_data() -> None:
    bars = _bars([10.0, 10.2])
    duplicate = DailyBar(
        trade_date=bars[0].trade_date,
        open_price=10.0,
        high_price=10.1,
        low_price=9.9,
        close_price=10.0,
        volume=100.0,
        turnover_amount=1000.0,
    )
    with pytest.raises(ValueError, match="重复交易日"):
        prepare_bars([bars[0], duplicate])

    invalid = DailyBar(
        trade_date=bars[0].trade_date + timedelta(days=3),
        open_price=10.0,
        high_price=9.0,
        low_price=9.5,
        close_price=9.8,
        volume=100.0,
        turnover_amount=1000.0,
    )
    with pytest.raises(ValueError, match="最高价"):
        prepare_bars([bars[0], invalid])
