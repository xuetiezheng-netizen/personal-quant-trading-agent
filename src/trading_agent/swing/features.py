"""少量、可解释的日线技术特征。

实现不依赖 pandas/numpy，方便在本地网页和测试中复用。所有窗口只向过去
看：第 ``t`` 根 K 线的值只使用 ``<= t`` 的数据；相对成交量的基准明确排除
当前 K 线，避免把当前异常放量同时当作基准。
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from datetime import date, datetime, time

from trading_agent.domain.models import DailyBar
from trading_agent.swing.models import SwingConfig, SwingFeatures

_MARKET_CLOSE = time(15, 0)


def _as_date(value: date | datetime) -> date:
    return value.date() if isinstance(value, datetime) else value


def _is_before_close(value: datetime) -> bool:
    # 没有时区转换：数据源和调用方应使用同一市场时区。15:00 后才允许把
    # as_of 当日的日线视为已收盘；date 截止则由调用者明确表示该日已完成。
    return value.timetz().replace(tzinfo=None) < _MARKET_CLOSE


def prepare_bars(
    bars: Iterable[DailyBar],
    *,
    as_of: date | datetime | None = None,
) -> tuple[DailyBar, ...]:
    """校验、排序并按截止时间截取已提供的日线。

    ``DailyBar`` 本身代表收盘数据。若 ``as_of`` 是 15:00 前的 datetime，
    同一天的 bar 会被排除，以防免费行情接口把盘中 bar 标成交易日收盘数据。
    若数据源没有时间信息，请在收盘后用 ``date`` 或 15:00 后的 datetime 调用。
    """

    materialized = list(bars)
    # 日线只按交易日排序；这样即使一个供应商带时区、另一个供应商不带
    # 时区，也不会因为同一市场的日期值触发 Python datetime 比较异常。
    ordered = sorted(materialized, key=lambda item: item.trade_date.date())
    seen_dates: set[date] = set()
    for bar in ordered:
        _validate_bar(bar)
        bar_date = bar.trade_date.date()
        if bar_date in seen_dates:
            raise ValueError(f"日线存在重复交易日: {bar_date.isoformat()}")
        seen_dates.add(bar_date)

    if as_of is None:
        return tuple(ordered)
    cutoff = _as_date(as_of)
    if isinstance(as_of, datetime) and _is_before_close(as_of):
        return tuple(bar for bar in ordered if bar.trade_date.date() < cutoff)
    return tuple(bar for bar in ordered if bar.trade_date.date() <= cutoff)


def _validate_bar(bar: DailyBar) -> None:
    values = (
        bar.open_price,
        bar.high_price,
        bar.low_price,
        bar.close_price,
        bar.volume,
        bar.turnover_amount,
    )
    if not all(math.isfinite(float(value)) for value in values):
        raise ValueError(f"日线包含非有限数值: {bar.trade_date!r}")
    if min(bar.open_price, bar.high_price, bar.low_price, bar.close_price) <= 0:
        raise ValueError(f"日线价格必须为正数: {bar.trade_date!r}")
    if bar.high_price < max(bar.open_price, bar.close_price):
        raise ValueError(f"日线最高价低于开盘价或收盘价: {bar.trade_date!r}")
    if bar.low_price > min(bar.open_price, bar.close_price):
        raise ValueError(f"日线最低价高于开盘价或收盘价: {bar.trade_date!r}")
    if bar.volume < 0 or bar.turnover_amount < 0:
        raise ValueError(f"成交量和成交额不能为负: {bar.trade_date!r}")


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def _std(values: Sequence[float]) -> float:
    average = _mean(values)
    return math.sqrt(sum((value - average) ** 2 for value in values) / len(values))


def _rolling_mean(values: Sequence[float], index: int, window: int) -> float | None:
    if index + 1 < window:
        return None
    return _mean(values[index + 1 - window : index + 1])


def _rsi(closes: Sequence[float], index: int, window: int) -> float | None:
    if index < window:
        return None
    changes = [closes[position] - closes[position - 1] for position in range(index - window + 1, index + 1)]
    gains = [max(change, 0.0) for change in changes]
    losses = [max(-change, 0.0) for change in changes]
    average_gain = _mean(gains)
    average_loss = _mean(losses)
    if average_loss == 0:
        return 100.0 if average_gain > 0 else 50.0
    relative_strength = average_gain / average_loss
    return 100.0 - 100.0 / (1.0 + relative_strength)


def _true_range(bars: Sequence[DailyBar], index: int) -> float | None:
    if index == 0:
        return None
    previous_close = bars[index - 1].close_price
    bar = bars[index]
    return max(
        bar.high_price - bar.low_price,
        abs(bar.high_price - previous_close),
        abs(bar.low_price - previous_close),
    )


def _atr(bars: Sequence[DailyBar], index: int, window: int) -> float | None:
    if index < window:
        return None
    ranges = [_true_range(bars, position) for position in range(index - window + 1, index + 1)]
    if any(value is None for value in ranges):
        return None
    return _mean([value for value in ranges if value is not None])


def _candle_patterns(bars: Sequence[DailyBar], index: int) -> tuple[str, ...]:
    bar = bars[index]
    candle_range = bar.high_price - bar.low_price
    if candle_range <= 0:
        return ()
    body = abs(bar.close_price - bar.open_price)
    upper_shadow = bar.high_price - max(bar.open_price, bar.close_price)
    lower_shadow = min(bar.open_price, bar.close_price) - bar.low_price
    body_ratio = body / candle_range
    upper_ratio = upper_shadow / candle_range
    lower_ratio = lower_shadow / candle_range
    patterns: list[str] = []

    # 形态只作为辅助确认，不单独决定状态。阈值宽松，避免过度精密化。
    if body_ratio <= 0.12 and upper_ratio >= 0.20 and lower_ratio >= 0.20:
        patterns.append("doji")
    if (
        body_ratio <= 0.40
        and lower_shadow >= max(body * 2.0, candle_range * 0.35)
        and upper_shadow <= max(body, candle_range * 0.15)
        and bar.close_price >= bar.open_price
    ):
        patterns.append("hammer")
    if (
        body_ratio <= 0.40
        and upper_shadow >= max(body * 2.0, candle_range * 0.35)
        and lower_shadow <= max(body, candle_range * 0.15)
        and bar.close_price <= bar.open_price
    ):
        patterns.append("shooting_star")

    if index > 0:
        previous = bars[index - 1]
        previous_body_low = min(previous.open_price, previous.close_price)
        previous_body_high = max(previous.open_price, previous.close_price)
        current_body_low = min(bar.open_price, bar.close_price)
        current_body_high = max(bar.open_price, bar.close_price)
        if (
            bar.close_price > bar.open_price
            and previous.close_price < previous.open_price
            and current_body_low <= previous_body_low
            and current_body_high >= previous_body_high
        ):
            patterns.append("bullish_engulfing")
        if (
            bar.close_price < bar.open_price
            and previous.close_price > previous.open_price
            and current_body_low <= previous_body_low
            and current_body_high >= previous_body_high
        ):
            patterns.append("bearish_engulfing")
    return tuple(patterns)


def _trend_regime(
    close: float,
    ma_fast: float | None,
    ma_slow: float | None,
    ma_fast_slope: float | None,
) -> str:
    if None in (ma_fast, ma_slow, ma_fast_slope):
        return "unknown"
    assert ma_fast is not None and ma_slow is not None and ma_fast_slope is not None
    if close >= ma_fast >= ma_slow and ma_fast_slope > 0:
        return "up"
    if close <= ma_fast <= ma_slow and ma_fast_slope < 0:
        return "down"
    return "sideways"


def calculate_swing_features(
    bars: Iterable[DailyBar],
    *,
    as_of: date | datetime | None = None,
    config: SwingConfig | None = None,
) -> tuple[SwingFeatures, ...]:
    """为每根可用日线计算只依赖当时及更早数据的特征。"""

    settings = config or SwingConfig()
    prepared = prepare_bars(bars, as_of=as_of)
    closes = [bar.close_price for bar in prepared]
    volumes = [bar.volume for bar in prepared]
    output: list[SwingFeatures] = []
    required = max(
        settings.min_history_bars,
        settings.price_position_window,
        settings.trend_slow_window + 1,
        settings.rsi_window + 1,
        settings.atr_window + 1,
        settings.bollinger_window,
        settings.relative_volume_window + 1,
    )

    for index, bar in enumerate(prepared):
        position_window = closes[max(0, index + 1 - settings.price_position_window) : index + 1]
        rolling_high = max(position_window)
        rolling_low = min(position_window)
        price_position = None
        if index + 1 >= settings.price_position_window:
            # 窗口内价格完全不变时没有真实的高低位差异；中位值比伪造
            # 极端低位或高位更稳妥，也让数据充足状态保持可解释。
            price_position = (
                (bar.close_price - rolling_low) / (rolling_high - rolling_low)
                if rolling_high > rolling_low
                else 0.5
            )
        drawdown = (
            bar.close_price / rolling_high - 1.0
            if index + 1 >= settings.price_position_window and rolling_high > 0
            else None
        )
        ma_fast = _rolling_mean(closes, index, settings.trend_fast_window)
        ma_slow = _rolling_mean(closes, index, settings.trend_slow_window)
        prior_ma_fast = _rolling_mean(closes, index - 1, settings.trend_fast_window) if index > 0 else None
        ma_fast_slope = ma_fast - prior_ma_fast if None not in (ma_fast, prior_ma_fast) else None
        rsi = _rsi(closes, index, settings.rsi_window)
        atr = _atr(prepared, index, settings.atr_window)
        atr_pct = atr / bar.close_price if atr is not None and bar.close_price > 0 else None

        bollinger_percent_b: float | None = None
        bollinger_bandwidth: float | None = None
        if index + 1 >= settings.bollinger_window:
            bb_values = closes[index + 1 - settings.bollinger_window : index + 1]
            bb_mean = _mean(bb_values)
            bb_std = _std(bb_values)
            upper = bb_mean + 2.0 * bb_std
            lower = bb_mean - 2.0 * bb_std
            band = upper - lower
            if band > 0:
                bollinger_percent_b = (bar.close_price - lower) / band
                bollinger_bandwidth = band / bb_mean if bb_mean else None
            else:
                # 价格完全不变时，位于布林带中部；这比伪造一个极端位置安全。
                bollinger_percent_b = 0.5
                bollinger_bandwidth = 0.0

        relative_volume: float | None = None
        if index >= settings.relative_volume_window:
            prior_volumes = volumes[index - settings.relative_volume_window : index]
            baseline = _mean(prior_volumes)
            if baseline > 0:
                relative_volume = bar.volume / baseline

        output.append(
            SwingFeatures(
                trade_date=bar.trade_date,
                close_price=bar.close_price,
                open_price=bar.open_price,
                high_price=bar.high_price,
                low_price=bar.low_price,
                volume=bar.volume,
                price_position=price_position,
                drawdown=drawdown,
                ma_fast=ma_fast,
                ma_slow=ma_slow,
                ma_fast_slope=ma_fast_slope,
                rsi=rsi,
                atr=atr,
                atr_pct=atr_pct,
                bollinger_percent_b=bollinger_percent_b,
                bollinger_bandwidth=bollinger_bandwidth,
                relative_volume=relative_volume,
                candle_patterns=_candle_patterns(prepared, index),
                trend_regime=_trend_regime(close=bar.close_price, ma_fast=ma_fast, ma_slow=ma_slow, ma_fast_slope=ma_fast_slope),
                bars_available=min(index + 1, required),
            )
        )
    return tuple(output)


def compute_features(
    bars: Iterable[DailyBar],
    *,
    as_of: date | datetime | None = None,
    config: SwingConfig | None = None,
) -> tuple[SwingFeatures, ...]:
    """``calculate_swing_features`` 的简短别名，供调用层使用。"""

    return calculate_swing_features(bars, as_of=as_of, config=config)


def latest_features(
    bars: Iterable[DailyBar],
    *,
    as_of: date | datetime | None = None,
    config: SwingConfig | None = None,
) -> SwingFeatures | None:
    """返回截止时点最后一根可用日线的特征。"""

    values = calculate_swing_features(bars, as_of=as_of, config=config)
    return values[-1] if values else None
