"""面向现有持仓的低换手波段观察状态机。"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
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
_PATTERN_LABELS = {
    "doji": "十字形态",
    "hammer": "锤子线",
    "bullish_engulfing": "看涨吞没形态",
    "shooting_star": "射击之星形态",
    "bearish_engulfing": "看跌吞没形态",
}
# These are rule parameters, not presentation-only values.  Keep them here so
# the state machine and the beginner explanation cannot silently drift apart.
LOW_BOLLINGER_PERCENT_B_THRESHOLD = 0.25
HIGH_BOLLINGER_PERCENT_B_THRESHOLD = 0.75

_STATE_LABELS = {
    SwingState.DATA_INSUFFICIENT: "数据不足",
    SwingState.LOW_WATCH: "低位观察",
    SwingState.BOTTOM_CONFIRMED: "低位反转信号",
    SwingState.NEUTRAL: "中性",
    SwingState.HIGH_WATCH: "高位观察",
    SwingState.TOP_CONFIRMED: "高位转弱信号",
}


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


def _finite_or_none(value: float | None) -> float | None:
    """Return a JSON-safe numeric observation for explanation payloads."""

    return value if value is not None and math.isfinite(value) else None


def _condition(
    key: str,
    label: str,
    actual: object,
    threshold: object,
    passed: bool,
    *,
    explanation: str = "",
) -> dict[str, object]:
    payload: dict[str, object] = {
        "key": key,
        "label": label,
        "actual": actual,
        "threshold": threshold,
        "pass": bool(passed),
    }
    if explanation:
        payload["explanation"] = explanation
    return payload


def _low_zone_conditions(features: SwingFeatures, config: SwingConfig) -> tuple[dict[str, object], ...]:
    position = _finite_or_none(features.price_position)
    drawdown = _finite_or_none(features.drawdown)
    rsi = _finite_or_none(features.rsi)
    bollinger = _finite_or_none(features.bollinger_percent_b)
    position_pass = position is not None and position <= config.low_position_threshold + _THRESHOLD_EPSILON
    drawdown_pass = drawdown is not None and drawdown <= config.low_drawdown_threshold + _THRESHOLD_EPSILON
    rsi_pass = rsi is not None and rsi <= config.low_rsi_threshold + _THRESHOLD_EPSILON
    bollinger_pass = bollinger is not None and bollinger <= LOW_BOLLINGER_PERCENT_B_THRESHOLD + _THRESHOLD_EPSILON
    return (
        _condition(
            "price_position",
            "区间位置偏低",
            position,
            {"operator": "≤", "value": config.low_position_threshold},
            position_pass,
            explanation="0 越接近区间低端，越像相对低位；不是绝对底部。",
        ),
        _condition(
            "drawdown",
            "相对滚动高点回撤",
            drawdown,
            {"operator": "≤", "value": config.low_drawdown_threshold},
            drawdown_pass,
            explanation="回撤为负，数值越低代表从近期高点回落越多。",
        ),
        _condition(
            "momentum_weak",
            "动能偏弱（RSI 或布林带位置）",
            {"rsi": rsi, "bollinger_percent_b": bollinger},
            {
                "logic": "任一满足",
                "rsi": {"operator": "≤", "value": config.low_rsi_threshold},
                "bollinger_percent_b": {
                    "operator": "≤",
                    "value": LOW_BOLLINGER_PERCENT_B_THRESHOLD,
                },
            },
            rsi_pass or bollinger_pass,
            explanation="RSI 或布林带位置任意一个偏弱即可；两者都缺失则不通过。",
        ),
    )


def _high_zone_conditions(features: SwingFeatures, config: SwingConfig) -> tuple[dict[str, object], ...]:
    position = _finite_or_none(features.price_position)
    drawdown = _finite_or_none(features.drawdown)
    rsi = _finite_or_none(features.rsi)
    bollinger = _finite_or_none(features.bollinger_percent_b)
    position_pass = position is not None and position >= config.high_position_threshold - _THRESHOLD_EPSILON
    drawdown_pass = drawdown is not None and drawdown >= config.high_drawdown_threshold - _THRESHOLD_EPSILON
    rsi_pass = rsi is not None and rsi >= config.high_rsi_threshold - _THRESHOLD_EPSILON
    bollinger_pass = bollinger is not None and bollinger >= HIGH_BOLLINGER_PERCENT_B_THRESHOLD - _THRESHOLD_EPSILON
    return (
        _condition(
            "price_position",
            "区间位置偏高",
            position,
            {"operator": "≥", "value": config.high_position_threshold},
            position_pass,
            explanation="1 越接近区间高端，越像相对高位；不是绝对顶部。",
        ),
        _condition(
            "drawdown",
            "距离滚动高点仍较近",
            drawdown,
            {"operator": "≥", "value": config.high_drawdown_threshold},
            drawdown_pass,
            explanation="回撤不深，表示价格仍靠近近期高点。",
        ),
        _condition(
            "momentum_strong",
            "动能偏强（RSI 或布林带位置）",
            {"rsi": rsi, "bollinger_percent_b": bollinger},
            {
                "logic": "任一满足",
                "rsi": {"operator": "≥", "value": config.high_rsi_threshold},
                "bollinger_percent_b": {
                    "operator": "≥",
                    "value": HIGH_BOLLINGER_PERCENT_B_THRESHOLD,
                },
            },
            rsi_pass or bollinger_pass,
            explanation="RSI 或布林带位置任意一个偏强即可；两者都缺失则不通过。",
        ),
    )


def _low_zone(features: SwingFeatures, config: SwingConfig) -> bool:
    return all(item["pass"] for item in _low_zone_conditions(features, config))


def _high_zone(features: SwingFeatures, config: SwingConfig) -> bool:
    return all(item["pass"] for item in _high_zone_conditions(features, config))


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


_TREND_LABELS = {
    "up": "上升",
    "down": "下降",
    "sideways": "震荡",
    "unknown": "未知",
}


def _state_label(state: SwingState) -> str:
    return _STATE_LABELS.get(state, state.value)


def _safe_pattern_names(features: SwingFeatures | None) -> list[str]:
    if features is None:
        return []
    return [_PATTERN_LABELS.get(name, name) for name in features.candle_patterns]


def _rsi_number_text(value: float | None) -> str:
    finite = _finite_or_none(value)
    return f"{finite:.1f}" if finite is not None else "未取得"


def _rsi_confirmation_actual(
    current: float | None,
    previous: float | None,
    tolerance: float,
    *,
    direction: str,
) -> dict[str, object]:
    if direction == "up":
        required = previous + tolerance if previous is not None else None
        comparison = (
            f"当前RSI {_rsi_number_text(current)} > 前一日RSI {_rsi_number_text(previous)} "
            f"+ {_rsi_number_text(tolerance)} = {_rsi_number_text(required)}"
        )
    else:
        required = previous - tolerance if previous is not None else None
        comparison = (
            f"当前RSI {_rsi_number_text(current)} < 前一日RSI {_rsi_number_text(previous)} "
            f"- {_rsi_number_text(tolerance)} = {_rsi_number_text(required)}"
        )
    return {
        "current_rsi": _finite_or_none(current),
        "previous_rsi": _finite_or_none(previous),
        "required_current_rsi": _finite_or_none(required),
        "comparison": comparison,
    }


def _comparison_condition(
    key: str,
    label: str,
    actual: object,
    threshold: object,
    passed: bool,
    *,
    explanation: str = "",
) -> dict[str, object]:
    """Build a condition with the same actual/threshold/pass contract."""

    return _condition(
        key,
        label,
        actual,
        threshold,
        passed,
        explanation=explanation,
    )


def _confirmation_path_explanation(
    features: SwingFeatures,
    *,
    previous: SwingFeatures | None,
    previous_state: SwingState | None,
    low_zone: bool,
    high_zone: bool,
    config: SwingConfig,
) -> dict[str, object]:
    """Explain both confirmation routes without inventing a new rule."""

    previous_state_value = previous_state.value if previous_state is not None else None
    no_doji = not _has_doji(features)
    bottom_trend = _bottom_trend_allows_confirmation(features)
    top_trend = _top_trend_allows_confirmation(features)
    previous_low = previous_state is SwingState.LOW_WATCH
    previous_high = previous_state is SwingState.HIGH_WATCH

    close_recovered = (
        previous is not None
        and features.close_price > previous.close_price + _THRESHOLD_EPSILON
    )
    rsi_turn = (
        previous is not None
        and features.rsi is not None
        and previous.rsi is not None
        and features.rsi > previous.rsi + config.reversal_rsi_tolerance
    )
    bullish_pattern = previous is not None and _has_any_pattern(
        previous.candle_patterns, _BULLISH_PATTERNS
    )
    pattern_breakout = (
        bullish_pattern
        and previous is not None
        and features.close_price > previous.high_price + _THRESHOLD_EPSILON
    )
    close_retreated = (
        previous is not None
        and features.close_price < previous.close_price - _THRESHOLD_EPSILON
    )
    top_rsi_turn = (
        previous is not None
        and features.rsi is not None
        and previous.rsi is not None
        and features.rsi < previous.rsi - config.reversal_rsi_tolerance
    )
    bearish_pattern = previous is not None and _has_any_pattern(
        previous.candle_patterns, _BEARISH_PATTERNS
    )
    pattern_breakdown = (
        bearish_pattern
        and previous is not None
        and features.close_price < previous.low_price - _THRESHOLD_EPSILON
    )

    bottom_eligible = previous_low and low_zone and no_doji and bottom_trend
    top_eligible = previous_high and high_zone and no_doji and top_trend
    bottom_momentum = close_recovered and rsi_turn
    top_momentum = close_retreated and top_rsi_turn
    # Keep the final route definition exactly aligned with the state machine:
    # eligibility gates are shared by both the pattern and momentum routes.
    bottom_confirmed = bottom_eligible and (pattern_breakout or bottom_momentum)
    top_confirmed = top_eligible and (pattern_breakdown or top_momentum)

    bottom_gates = (
        _comparison_condition(
            "previous_low_watch",
            "前一日处于低位观察",
            previous_state_value,
            SwingState.LOW_WATCH.value,
            previous_low,
        ),
        _comparison_condition(
            "current_low_zone",
            "当前仍满足低位三项条件",
            low_zone,
            True,
            low_zone,
        ),
        _comparison_condition(
            "trend_allows_bottom",
            "趋势环境允许观察回稳",
            features.trend_regime,
            ["up", "sideways"],
            bottom_trend,
        ),
        _comparison_condition(
            "not_doji",
            "当前不是单独的十字犹豫日",
            _safe_pattern_names(features),
            "不含 doji",
            no_doji,
        ),
    )
    top_gates = (
        _comparison_condition(
            "previous_high_watch",
            "前一日处于高位观察",
            previous_state_value,
            SwingState.HIGH_WATCH.value,
            previous_high,
        ),
        _comparison_condition(
            "current_high_zone",
            "当前仍满足高位三项条件",
            high_zone,
            True,
            high_zone,
        ),
        _comparison_condition(
            "trend_allows_top",
            "趋势环境允许观察转弱",
            features.trend_regime,
            ["down", "sideways"],
            top_trend,
        ),
        _comparison_condition(
            "not_doji",
            "当前不是单独的十字犹豫日",
            _safe_pattern_names(features),
            "不含 doji",
            no_doji,
        ),
    )
    return {
        "previous_state": {
            "value": previous_state_value,
            "label": _state_label(previous_state) if previous_state is not None else "无前一日状态",
        },
        "bottom": {
            "eligible_conditions": list(bottom_gates),
            "pass": bottom_confirmed,
            "routes": [
                {
                    "key": "pattern_breakout",
                    "label": "形态突破路径",
                    "actual": {
                        "previous_patterns": _safe_pattern_names(previous),
                        "current_close": _finite_or_none(features.close_price),
                        "previous_high": _finite_or_none(previous.high_price) if previous else None,
                    },
                    "threshold": "前一日有锤子线/看涨吞没，且当前收盘高于前一日最高价",
                    "pass": bool(bottom_eligible and pattern_breakout),
                },
                {
                    "key": "momentum_improvement",
                    "label": "动能改善路径",
                    "actual": {
                        "current_close": _finite_or_none(features.close_price),
                        "previous_close": _finite_or_none(previous.close_price) if previous else None,
                        "rsi_chain": _rsi_confirmation_actual(
                            features.rsi,
                            previous.rsi if previous else None,
                            config.reversal_rsi_tolerance,
                            direction="up",
                        ),
                    },
                    "threshold": {
                        "price": "当前收盘高于前一日收盘",
                        "rsi": {
                            "operator": ">",
                            "value": config.reversal_rsi_tolerance,
                            "formula": f"当前RSI > 前一日RSI + {_rsi_number_text(config.reversal_rsi_tolerance)}",
                            "meaning": "相对前一日改善超过容差",
                        },
                    },
                    "pass": bool(bottom_eligible and bottom_momentum),
                },
            ],
        },
        "top": {
            "eligible_conditions": list(top_gates),
            "pass": top_confirmed,
            "routes": [
                {
                    "key": "pattern_breakdown",
                    "label": "形态跌破路径",
                    "actual": {
                        "previous_patterns": _safe_pattern_names(previous),
                        "current_close": _finite_or_none(features.close_price),
                        "previous_low": _finite_or_none(previous.low_price) if previous else None,
                    },
                    "threshold": "前一日有射击之星/看跌吞没，且当前收盘低于前一日最低价",
                    "pass": bool(top_eligible and pattern_breakdown),
                },
                {
                    "key": "momentum_weakening",
                    "label": "动能转弱路径",
                    "actual": {
                        "current_close": _finite_or_none(features.close_price),
                        "previous_close": _finite_or_none(previous.close_price) if previous else None,
                        "rsi_chain": _rsi_confirmation_actual(
                            features.rsi,
                            previous.rsi if previous else None,
                            config.reversal_rsi_tolerance,
                            direction="down",
                        ),
                    },
                    "threshold": {
                        "price": "当前收盘低于前一日收盘",
                        "rsi": {
                            "operator": "<",
                            "value": config.reversal_rsi_tolerance,
                            "formula": f"当前RSI < 前一日RSI - {_rsi_number_text(config.reversal_rsi_tolerance)}",
                            "meaning": "相对前一日走弱超过容差",
                        },
                    },
                    "pass": bool(top_eligible and top_momentum),
                },
            ],
        },
    }


def _conclusion_text(
    state: SwingState,
    *,
    low_conditions: tuple[dict[str, object], ...],
    high_conditions: tuple[dict[str, object], ...],
    confirmation: Mapping[str, object],
    enough_history: bool,
) -> tuple[str, str, str]:
    low_passed = sum(1 for item in low_conditions if item["pass"])
    high_passed = sum(1 for item in high_conditions if item["pass"])
    bottom = confirmation.get("bottom") if isinstance(confirmation, Mapping) else None
    top = confirmation.get("top") if isinstance(confirmation, Mapping) else None
    bottom_pass = bool(bottom.get("pass")) if isinstance(bottom, Mapping) else False
    top_pass = bool(top.get("pass")) if isinstance(top, Mapping) else False
    if not enough_history or state is SwingState.DATA_INSUFFICIENT:
        why = "有效收盘日线或指标预热不足，因此不把当前价格归入低位或高位结论。"
    elif state is SwingState.BOTTOM_CONFIRMED:
        why = "低位三项条件已满足，并且此前低位观察后出现了价格/动能或形态突破确认。"
    elif state is SwingState.TOP_CONFIRMED:
        why = "高位三项条件已满足，并且此前高位观察后出现了价格/动能或形态转弱确认。"
    elif state is SwingState.LOW_WATCH:
        why = "低位三项条件已满足，但后续确认路径尚未同时通过，所以仍是观察状态。"
    elif state is SwingState.HIGH_WATCH:
        why = "高位三项条件已满足，但后续确认路径尚未同时通过，所以仍是观察状态。"
    elif low_passed == 3 and high_passed == 3:
        why = "低位和高位三项条件同时通过，条件相互冲突，因此保持中性，不输出方向结论。"
    else:
        why = (
            f"低位条件通过 {low_passed}/3，尚未全部满足；高位条件通过 {high_passed}/3，"
            "也尚未全部满足，因此当前保持中性。"
        )
        if bottom_pass or top_pass:
            why += "确认路径虽有局部迹象，但未形成可用的完整状态。"
    why_not_low = (
        "低位三项条件已全部通过。"
        if low_passed == 3
        else f"低位观察未成立：三项中仅 {low_passed}/3 通过，至少有一项未达到阈值。"
    )
    why_not_high = (
        "高位三项条件已全部通过。"
        if high_passed == 3
        else f"高位观察未成立：三项中仅 {high_passed}/3 通过，至少有一项未达到阈值。"
    )
    return why, why_not_low, why_not_high


def _indicator_snapshot(features: SwingFeatures | None) -> dict[str, object]:
    if features is None:
        return {}
    return {
        "open": _finite_or_none(features.open_price),
        "high": _finite_or_none(features.high_price),
        "low": _finite_or_none(features.low_price),
        "close": _finite_or_none(features.close_price),
        "volume": _finite_or_none(features.volume),
        "price_position": _finite_or_none(features.price_position),
        "drawdown": _finite_or_none(features.drawdown),
        "ma_fast": _finite_or_none(features.ma_fast),
        "ma_slow": _finite_or_none(features.ma_slow),
        "ma_fast_slope": _finite_or_none(features.ma_fast_slope),
        "rsi": _finite_or_none(features.rsi),
        "atr": _finite_or_none(features.atr),
        "atr_pct": _finite_or_none(features.atr_pct),
        "bollinger_percent_b": _finite_or_none(features.bollinger_percent_b),
        "bollinger_bandwidth": _finite_or_none(features.bollinger_bandwidth),
        "relative_volume": _finite_or_none(features.relative_volume),
        "trend_regime": (
            features.trend_regime if features.trend_regime in _TREND_LABELS else "unknown"
        ),
        "candle_patterns": _safe_pattern_names(features),
        "bars_available": features.bars_available,
    }


def _insufficient_explanation(
    features: SwingFeatures | None,
) -> dict[str, object]:
    """Short-circuit all directional rules when history is insufficient."""

    trend_actual = (
        {
            "close": _finite_or_none(features.close_price),
            "ma_fast": _finite_or_none(features.ma_fast),
            "ma_slow": _finite_or_none(features.ma_slow),
            "ma_fast_slope": _finite_or_none(features.ma_fast_slope),
        }
        if features is not None
        else {}
    )
    boundary = {
        "text": "有效日线不足，未评估低位、高位或趋势；不能把缺失数据解释成方向信号。",
        "strict_invalidation_rule": "没有设置严格失效规则；数据恢复并达到最低历史长度后重新计算。",
        "not_a_trade_instruction": "建议仅用于机动仓研究，核心仓不因技术信号自动改变，也不会生成自动交易命令。",
    }
    recommendation = "暂不形成机动仓研究结论，先补足有效收盘日线；核心仓不因数据不足自动改变。"
    return {
        "schema_version": 1,
        "threshold_source": "SwingConfig + strategy.py 规则常量",
        "analysis_flow": [
            "先检查有效收盘日线数量和指标预热是否达到最低要求。",
            "当前数据不足，低位、高位、趋势和确认路径均未评估。",
        ],
        "low_watch": {"pass": False, "evaluated": False, "conditions": []},
        "high_watch": {"pass": False, "evaluated": False, "conditions": []},
        "low_watch_conditions": [],
        "high_watch_conditions": [],
        "trend_environment": {
            "value": None,
            "label": "未评估",
            "actual": trend_actual,
            "threshold": "上升：收盘≥短均线≥长均线且短均线斜率>0；下降为相反；否则震荡。",
            "explanation": "有效日线不足，趋势未评估。",
        },
        "confirmation_path": {"evaluated": False},
        "current_state": {"value": SwingState.DATA_INSUFFICIENT.value, "label": _state_label(SwingState.DATA_INSUFFICIENT)},
        "conclusion": {
            "state": SwingState.DATA_INSUFFICIENT.value,
            "state_label": _state_label(SwingState.DATA_INSUFFICIENT),
            "why": "有效收盘日线不足，因此只标记为数据不足，不生成低位、高位或方向结论。",
            "why_not_low": "有效收盘日线不足，低位条件未评估。",
            "why_not_high": "有效收盘日线不足，高位条件未评估。",
        },
        "research_recommendation": recommendation,
        "recommendation": recommendation,
        "next_observation": "有效日线数量是否达到最低要求，且各指标能够完整计算。",
        "model_boundary": boundary,
        "indicator_snapshot": _indicator_snapshot(features),
    }


def build_analysis_explanation(
    features: SwingFeatures | None,
    *,
    decision: SwingDecision | None = None,
    previous: SwingFeatures | None = None,
    previous_state: SwingState | None = None,
    config: SwingConfig | None = None,
    enough_history: bool | None = None,
) -> dict[str, object]:
    """Build the structured, beginner-friendly explanation for one result.

    The state machine remains the only source of thresholds.  This function only
    reads the same condition helpers and turns their result into an inspectable
    explanation for the Web/Markdown surfaces.
    """

    settings = config or SwingConfig()
    state = decision.state if decision is not None else SwingState.DATA_INSUFFICIENT
    state_label = _state_label(state)
    base_sufficient = (
        features is not None
        and features.is_complete
        and features.bars_available >= settings.min_history_bars
    )
    # An explicit flag may tighten the caller's assessment, but can never
    # override the feature contract or a state-machine data-insufficient state.
    sufficient = base_sufficient and enough_history is not False
    if state is SwingState.DATA_INSUFFICIENT:
        sufficient = False
    if not sufficient:
        return _insufficient_explanation(features)

    low_conditions = _low_zone_conditions(features, settings)
    high_conditions = _high_zone_conditions(features, settings)
    low_pass = all(item["pass"] for item in low_conditions)
    high_pass = all(item["pass"] for item in high_conditions)
    confirmation = _confirmation_path_explanation(
        features,
        previous=previous,
        previous_state=previous_state,
        low_zone=low_pass,
        high_zone=high_pass,
        config=settings,
    )
    why, why_not_low, why_not_high = _conclusion_text(
        state,
        low_conditions=low_conditions,
        high_conditions=high_conditions,
        confirmation=confirmation,
        enough_history=sufficient,
    )
    if state is SwingState.BOTTOM_CONFIRMED:
        recommendation = "将机动仓列为重点回稳跟踪对象，观察后续收盘是否延续；核心仓不因技术信号自动改变。"
        next_observation = "价格是否继续保持在确认方向，RSI 是否没有重新走弱。"
    elif state is SwingState.TOP_CONFIRMED:
        recommendation = "将机动仓列为重点转弱跟踪对象，观察后续收盘是否延续；核心仓不因技术信号自动改变。"
        next_observation = "价格是否继续走弱，RSI 是否没有重新转强。"
    elif state is SwingState.LOW_WATCH:
        recommendation = "仅把机动仓列入低位研究观察，等待后续收盘确认；核心仓保持原长期逻辑。"
        next_observation = "是否出现价格回稳并且 RSI 改善，或突破观察日前高。"
    elif state is SwingState.HIGH_WATCH:
        recommendation = "仅把机动仓列入高位风险观察，等待后续收盘确认；核心仓保持原长期逻辑。"
        next_observation = "是否出现价格走弱并且 RSI 回落，或跌破观察日前低。"
    else:
        recommendation = "暂不把机动仓调整作为研究结论，继续按收盘节奏观察；核心仓不因中性状态自动改变。"
        next_observation = "低位或高位三项条件是否形成一致，随后是否出现确认路径。"

    trend_value = features.trend_regime if features.trend_regime in _TREND_LABELS else "unknown"
    boundary = {
        "text": "模型只使用收盘价、成交量、均线、RSI、布林带和少量 K 线形态，不使用基本面、新闻或资金流。",
        "strict_invalidation_rule": "没有设置严格失效规则；观察状态会随新的已收盘日线重算，不把单日波动直接当成失效。",
        "not_a_trade_instruction": "建议仅用于机动仓研究，核心仓不因技术信号自动改变，也不会生成自动交易命令。",
    }
    indicators = _indicator_snapshot(features)
    return {
        "schema_version": 1,
        "threshold_source": "SwingConfig + strategy.py 规则常量",
        "analysis_flow": [
            "只使用已经收盘的日线，并先检查历史长度和指标是否完整。",
            f"把最新价格放进最近 {settings.price_position_window} 个交易日区间，检查区间位置和相对回撤。",
            f"用 {settings.trend_fast_window} 日/{settings.trend_slow_window} 日均线及短均线斜率判断趋势环境。",
            f"用 {settings.rsi_window} 日 RSI、{settings.bollinger_window} 日布林带、成交量和 K 线形态作辅助。",
            "低位或高位三项条件先形成观察区，再要求下一日线出现确认；否则保持观察或中性。",
        ],
        "low_watch": {"pass": low_pass, "conditions": list(low_conditions)},
        "high_watch": {"pass": high_pass, "conditions": list(high_conditions)},
        "low_watch_conditions": list(low_conditions),
        "high_watch_conditions": list(high_conditions),
        "trend_environment": {
            "value": trend_value,
            "label": _TREND_LABELS[trend_value],
            "actual": {
                "close": _finite_or_none(features.close_price),
                "ma_fast": _finite_or_none(features.ma_fast),
                "ma_slow": _finite_or_none(features.ma_slow),
                "ma_fast_slope": _finite_or_none(features.ma_fast_slope),
            },
            "threshold": "上升：收盘≥短均线≥长均线且短均线斜率>0；下降为相反；否则震荡。",
            "explanation": "趋势只用于限制确认方向，不单独决定高低位。",
        },
        "confirmation_path": confirmation,
        "current_state": {
            "value": state.value,
            "label": state_label,
            "confidence": decision.confidence if decision is not None else "low",
        },
        "conclusion": {
            "state": state.value,
            "state_label": state_label,
            "why": why,
            "why_not_low": why_not_low,
            "why_not_high": why_not_high,
        },
        "research_recommendation": recommendation,
        "recommendation": recommendation,
        "next_observation": next_observation,
        "model_boundary": boundary,
        "indicator_snapshot": indicators,
    }


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
