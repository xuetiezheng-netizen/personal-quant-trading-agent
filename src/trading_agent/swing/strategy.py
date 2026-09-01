"""面向现有持仓的低换手波段观察状态机。"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from trading_agent.swing.models import (
    SwingConfig,
    SwingDecision,
    SwingFeatures,
    SwingState,
)

# 十字线只表示犹豫，不作为方向性证据。方向形态必须先出现在观察日，
# 再由下一根收盘价完成突破，避免用一根 K 线直接宣称发生了拐点。
_BULLISH_PATTERNS = {"hammer", "bullish_engulfing"}
_BEARISH_PATTERNS = {"shooting_star", "bearish_engulfing"}
_DOJI_PATTERN = "doji"
_THRESHOLD_EPSILON = 1e-9


def _has_any_pattern(patterns: tuple[str, ...], candidates: set[str]) -> bool:
    return bool(set(patterns).intersection(candidates))


def _has_doji(features: SwingFeatures) -> bool:
    return _DOJI_PATTERN in features.candle_patterns


def _bottom_trend_allows_confirmation(features: SwingFeatures) -> bool:
    """底部信号只在已知、非下行的当前趋势环境中成立。"""

    return features.trend_regime in {"up", "sideways"}


def _top_trend_allows_confirmation(features: SwingFeatures) -> bool:
    """顶部信号只在已知、非上行的当前趋势环境中成立。"""

    return features.trend_regime in {"down", "sideways"}


def _low_zone(features: SwingFeatures, config: SwingConfig) -> bool:
    if features.price_position is None or features.drawdown is None:
        return False
    momentum_weak = (
        features.rsi is not None and features.rsi <= config.low_rsi_threshold
    ) or (
        features.bollinger_percent_b is not None and features.bollinger_percent_b <= 0.25
    )
    return (
        features.price_position <= config.low_position_threshold + _THRESHOLD_EPSILON
        and features.drawdown <= config.low_drawdown_threshold + _THRESHOLD_EPSILON
        and momentum_weak
    )


def _high_zone(features: SwingFeatures, config: SwingConfig) -> bool:
    if features.price_position is None or features.drawdown is None:
        return False
    momentum_strong = (
        features.rsi is not None and features.rsi >= config.high_rsi_threshold
    ) or (
        features.bollinger_percent_b is not None and features.bollinger_percent_b >= 0.75
    )
    return (
        features.price_position >= config.high_position_threshold - _THRESHOLD_EPSILON
        and features.drawdown >= config.high_drawdown_threshold - _THRESHOLD_EPSILON
        and momentum_strong
    )


def _bottom_confirmation(
    features: SwingFeatures,
    previous: SwingFeatures | None,
    previous_state: SwingState | None,
    config: SwingConfig,
) -> bool:
    if (
        previous is None
        or previous_state is not SwingState.LOW_WATCH
        or not _low_zone(features, config)
        or _has_doji(features)
        or not _bottom_trend_allows_confirmation(features)
    ):
        return False
    close_recovered = features.close_price > previous.close_price + _THRESHOLD_EPSILON
    rsi_turn = (
        features.rsi is not None
        and previous.rsi is not None
        and features.rsi > previous.rsi + config.reversal_rsi_tolerance
    )
    # 形态路线：观察日出现方向形态，确认日收盘必须真正越过观察日最高价。
    pattern_confirmation = _has_any_pattern(previous.candle_patterns, _BULLISH_PATTERNS) and (
        features.close_price > previous.high_price + _THRESHOLD_EPSILON
    )
    # 动量路线：不依赖某个形态，但必须同时看到价格和 RSI 的同向改善。
    momentum_confirmation = close_recovered and rsi_turn
    return pattern_confirmation or momentum_confirmation


def _top_confirmation(
    features: SwingFeatures,
    previous: SwingFeatures | None,
    previous_state: SwingState | None,
    config: SwingConfig,
) -> bool:
    if (
        previous is None
        or previous_state is not SwingState.HIGH_WATCH
        or not _high_zone(features, config)
        or _has_doji(features)
        or not _top_trend_allows_confirmation(features)
    ):
        return False
    close_retreated = features.close_price < previous.close_price - _THRESHOLD_EPSILON
    rsi_turn = (
        features.rsi is not None
        and previous.rsi is not None
        and features.rsi < previous.rsi - config.reversal_rsi_tolerance
    )
    # 形态路线：观察日出现方向形态，确认日收盘必须真正跌破观察日最低价。
    pattern_confirmation = _has_any_pattern(previous.candle_patterns, _BEARISH_PATTERNS) and (
        features.close_price < previous.low_price - _THRESHOLD_EPSILON
    )
    # 动量路线：不依赖某个形态，但必须同时看到价格和 RSI 的同向走弱。
    momentum_confirmation = close_retreated and rsi_turn
    return pattern_confirmation or momentum_confirmation


def evaluate_swing_state(
    features: SwingFeatures,
    *,
    previous: SwingFeatures | None = None,
    previous_state: SwingState | None = None,
    config: SwingConfig | None = None,
) -> SwingDecision:
    """在一个收盘时点评估观察状态。

    信号在当前 ``trade_date`` 收盘形成；本函数不产生执行日期，也不产生买卖
    文案。历史回放若要使用确认状态，必须显式在下一根 K 线开盘成交。
    """

    settings = config or SwingConfig()
    insufficient = not features.is_complete or features.bars_available < settings.min_history_bars
    if insufficient:
        return SwingDecision(
            trade_date=features.trade_date,
            state=SwingState.DATA_INSUFFICIENT,
            features=features,
            confidence="low",
            reasons=("history_or_indicator_insufficient",),
            tactical_target=None,
        )

    is_low = _low_zone(features, settings)
    is_high = _high_zone(features, settings)
    bottom = _bottom_confirmation(features, previous, previous_state, settings)
    top = _top_confirmation(features, previous, previous_state, settings)

    # 正常 OHLC 不会同时处于高低区；若数据或自定义阈值造成同时满足，宁可
    # 返回中性，避免让一个含糊的状态影响历史回放。
    if is_low and is_high:
        return SwingDecision(
            trade_date=features.trade_date,
            state=SwingState.NEUTRAL,
            features=features,
            confidence="low",
            reasons=("ambiguous_extreme_zone",),
            tactical_target=None,
        )

    if bottom and not top:
        return SwingDecision(
            trade_date=features.trade_date,
            state=SwingState.BOTTOM_CONFIRMED,
            features=features,
            confidence="medium",
            reasons=("low_price_position", "drawdown", "reversal_confirmation"),
            tactical_target=1.0,
        )
    if top and not bottom:
        return SwingDecision(
            trade_date=features.trade_date,
            state=SwingState.TOP_CONFIRMED,
            features=features,
            confidence="medium",
            reasons=("high_price_position", "near_rolling_high", "reversal_confirmation"),
            tactical_target=0.0,
        )
    if is_low:
        return SwingDecision(
            trade_date=features.trade_date,
            state=SwingState.LOW_WATCH,
            features=features,
            confidence="low",
            reasons=("low_price_position", "reversal_not_confirmed"),
            tactical_target=None,
        )
    if is_high:
        return SwingDecision(
            trade_date=features.trade_date,
            state=SwingState.HIGH_WATCH,
            features=features,
            confidence="low",
            reasons=("high_price_position", "reversal_not_confirmed"),
            tactical_target=None,
        )
    return SwingDecision(
        trade_date=features.trade_date,
        state=SwingState.NEUTRAL,
        features=features,
        confidence="low",
        reasons=("no_extreme_or_confirmation",),
        tactical_target=None,
    )


class SwingStateMachine:
    """按收盘日顺序处理特征，保留上一个状态用于反转确认。"""

    def __init__(self, config: SwingConfig | None = None) -> None:
        self.config = config or SwingConfig()
        self.reset()

    def reset(self) -> None:
        self._previous_features: SwingFeatures | None = None
        self._previous_state: SwingState | None = None
        self._last_date: datetime | None = None

    @property
    def state(self) -> SwingState | None:
        return self._previous_state

    def update(self, features: SwingFeatures) -> SwingDecision:
        if self._last_date is not None and features.trade_date <= self._last_date:
            raise ValueError("状态机必须按严格递增的交易日输入特征")
        decision = evaluate_swing_state(
            features,
            previous=self._previous_features,
            previous_state=self._previous_state,
            config=self.config,
        )
        self._previous_features = features
        self._previous_state = decision.state
        self._last_date = features.trade_date
        return decision

    def evaluate(self, features: Iterable[SwingFeatures]) -> tuple[SwingDecision, ...]:
        """从头开始处理一组按日期排列的特征。"""

        self.reset()
        return tuple(self.update(item) for item in features)


def evaluate_swing_series(
    features: Iterable[SwingFeatures], *, config: SwingConfig | None = None
) -> tuple[SwingDecision, ...]:
    """无状态调用层的便捷入口。"""

    return SwingStateMachine(config).evaluate(features)
