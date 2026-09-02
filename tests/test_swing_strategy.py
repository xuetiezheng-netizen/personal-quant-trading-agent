from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from trading_agent.swing.models import SwingConfig, SwingFeatures, SwingState
from trading_agent.swing.strategy import (
    HIGH_BOLLINGER_PERCENT_B_THRESHOLD,
    LOW_BOLLINGER_PERCENT_B_THRESHOLD,
    SwingStateMachine,
    build_analysis_explanation,
    evaluate_swing_state,
)


def _feature(
    index: int,
    *,
    close: float = 100.0,
    position: float = 0.50,
    drawdown: float = -0.03,
    rsi: float = 50.0,
    bb: float = 0.50,
    patterns: tuple[str, ...] = (),
    trend: str = "sideways",
    bars_available: int = 120,
) -> SwingFeatures:
    return SwingFeatures(
        trade_date=datetime(2025, 1, 1, tzinfo=UTC) + timedelta(days=index),
        close_price=close,
        open_price=close,
        high_price=close + 1.0,
        low_price=close - 1.0,
        volume=100.0,
        price_position=position,
        drawdown=drawdown,
        ma_fast=close,
        ma_slow=close,
        ma_fast_slope=0.1,
        rsi=rsi,
        atr=1.0,
        atr_pct=0.01,
        bollinger_percent_b=bb,
        bollinger_bandwidth=0.05,
        relative_volume=1.0,
        candle_patterns=patterns,
        trend_regime=trend,
        bars_available=bars_available,
    )


@pytest.fixture
def strategy_config() -> SwingConfig:
    return SwingConfig(min_history_bars=120)


def test_state_machine_has_watch_then_confirmed_bottom_without_order_language(strategy_config) -> None:
    previous = _feature(
        0,
        close=90.0,
        position=0.10,
        drawdown=-0.30,
        rsi=25.0,
        bb=0.10,
        patterns=("hammer",),
    )
    current = _feature(
        1,
        close=92.0,
        position=0.12,
        drawdown=-0.29,
        rsi=25.0,
        bb=0.15,
    )

    watch = evaluate_swing_state(previous, config=strategy_config)
    confirmed = evaluate_swing_state(
        current,
        previous=previous,
        previous_state=watch.state,
        config=strategy_config,
    )

    assert watch.state is SwingState.LOW_WATCH
    assert watch.tactical_target is None
    assert confirmed.state is SwingState.BOTTOM_CONFIRMED
    assert confirmed.tactical_target == 1.0
    assert confirmed.is_observation_only
    assert "buy" not in repr(confirmed).lower()
    assert "sell" not in repr(confirmed).lower()


def test_state_machine_has_watch_then_confirmed_top(strategy_config) -> None:
    previous = _feature(
        0,
        close=110.0,
        position=0.90,
        drawdown=-0.01,
        rsi=75.0,
        bb=0.90,
        patterns=("shooting_star",),
    )
    current = _feature(
        1,
        close=108.0,
        position=0.88,
        drawdown=-0.02,
        rsi=75.0,
        bb=0.85,
    )

    watch = evaluate_swing_state(previous, config=strategy_config)
    confirmed = evaluate_swing_state(
        current,
        previous=previous,
        previous_state=watch.state,
        config=strategy_config,
    )

    assert watch.state is SwingState.HIGH_WATCH
    assert confirmed.state is SwingState.TOP_CONFIRMED
    assert confirmed.tactical_target == 0.0


@pytest.mark.parametrize(
    ("side", "watch_state", "confirmed_state", "previous", "current"),
    [
        (
            "bottom",
            SwingState.LOW_WATCH,
            SwingState.BOTTOM_CONFIRMED,
            _feature(
                0,
                close=90.0,
                position=0.10,
                drawdown=-0.30,
                rsi=25.0,
                bb=0.10,
                patterns=("hammer",),
            ),
            _feature(1, close=92.0, position=0.12, drawdown=-0.29, rsi=24.0, bb=0.15),
        ),
        (
            "top",
            SwingState.HIGH_WATCH,
            SwingState.TOP_CONFIRMED,
            _feature(
                0,
                close=110.0,
                position=0.90,
                drawdown=-0.01,
                rsi=75.0,
                bb=0.90,
                patterns=("shooting_star",),
            ),
            _feature(1, close=108.0, position=0.88, drawdown=-0.02, rsi=76.0, bb=0.85),
        ),
    ],
)
def test_previous_state_changes_the_confirmation_result(
    side,
    watch_state,
    confirmed_state,
    previous,
    current,
    strategy_config,
) -> None:
    confirmed = evaluate_swing_state(
        current,
        previous=previous,
        previous_state=watch_state,
        config=strategy_config,
    )
    without_watch = evaluate_swing_state(
        current,
        previous=previous,
        previous_state=SwingState.NEUTRAL,
        config=strategy_config,
    )

    assert confirmed.state is confirmed_state
    assert without_watch.state is not confirmed_state
    assert side in {"bottom", "top"}


@pytest.mark.parametrize(
    ("previous", "current", "expected_watch"),
    [
        (
            _feature(
                0,
                close=90.0,
                position=0.10,
                drawdown=-0.30,
                rsi=25.0,
                bb=0.10,
                patterns=("hammer",),
            ),
            _feature(1, close=90.5, position=0.12, drawdown=-0.29, rsi=24.0, bb=0.15),
            SwingState.LOW_WATCH,
        ),
        (
            _feature(
                0,
                close=110.0,
                position=0.90,
                drawdown=-0.01,
                rsi=75.0,
                bb=0.90,
                patterns=("shooting_star",),
            ),
            _feature(1, close=109.5, position=0.88, drawdown=-0.02, rsi=76.0, bb=0.85),
            SwingState.HIGH_WATCH,
        ),
    ],
)
def test_directional_pattern_needs_next_close_breakout(
    previous,
    current,
    expected_watch,
    strategy_config,
) -> None:
    watch = evaluate_swing_state(previous, config=strategy_config)
    decision = evaluate_swing_state(
        current,
        previous=previous,
        previous_state=watch.state,
        config=strategy_config,
    )

    assert watch.state is expected_watch
    assert decision.state is expected_watch


@pytest.mark.parametrize(
    ("previous", "current", "watch_state"),
    [
        (
            _feature(0, close=90.0, position=0.10, drawdown=-0.30, rsi=25.0, bb=0.10),
            _feature(1, close=91.0, position=0.12, drawdown=-0.29, rsi=29.0, bb=0.15),
            SwingState.LOW_WATCH,
        ),
        (
            _feature(0, close=110.0, position=0.90, drawdown=-0.01, rsi=75.0, bb=0.90),
            _feature(1, close=109.0, position=0.88, drawdown=-0.02, rsi=70.0, bb=0.85),
            SwingState.HIGH_WATCH,
        ),
    ],
)
def test_single_day_pulse_without_prior_watch_cannot_confirm(
    previous,
    current,
    watch_state,
    strategy_config,
) -> None:
    decision = evaluate_swing_state(
        current,
        previous=previous,
        previous_state=SwingState.NEUTRAL,
        config=strategy_config,
    )

    assert decision.state is watch_state


@pytest.mark.parametrize(
    ("previous", "current", "watch_state"),
    [
        (
            _feature(
                0,
                close=90.0,
                position=0.10,
                drawdown=-0.30,
                rsi=25.0,
                bb=0.10,
                patterns=("hammer",),
            ),
            _feature(
                1,
                close=92.0,
                position=0.12,
                drawdown=-0.29,
                rsi=25.0,
                bb=0.15,
                patterns=("doji",),
            ),
            SwingState.LOW_WATCH,
        ),
        (
            _feature(
                0,
                close=110.0,
                position=0.90,
                drawdown=-0.01,
                rsi=75.0,
                bb=0.90,
                patterns=("shooting_star",),
            ),
            _feature(
                1,
                close=108.0,
                position=0.88,
                drawdown=-0.02,
                rsi=75.0,
                bb=0.85,
                patterns=("doji",),
            ),
            SwingState.HIGH_WATCH,
        ),
    ],
)
def test_doji_only_is_watch_only(
    previous,
    current,
    watch_state,
    strategy_config,
) -> None:
    decision = evaluate_swing_state(
        current,
        previous=previous,
        previous_state=watch_state,
        config=strategy_config,
    )

    assert decision.state is watch_state
    assert decision.tactical_target is None


@pytest.mark.parametrize(
    ("previous", "current", "watch_state"),
    [
        (
            _feature(
                0,
                close=90.0,
                position=0.10,
                drawdown=-0.30,
                rsi=25.0,
                bb=0.10,
                patterns=("hammer",),
            ),
            _feature(1, close=92.0, position=0.12, drawdown=-0.29, rsi=25.0, bb=0.15, trend="unknown"),
            SwingState.LOW_WATCH,
        ),
        (
            _feature(
                0,
                close=110.0,
                position=0.90,
                drawdown=-0.01,
                rsi=75.0,
                bb=0.90,
                patterns=("shooting_star",),
            ),
            _feature(1, close=108.0, position=0.88, drawdown=-0.02, rsi=75.0, bb=0.85, trend="unknown"),
            SwingState.HIGH_WATCH,
        ),
    ],
)
def test_unknown_trend_cannot_confirm(
    previous,
    current,
    watch_state,
    strategy_config,
) -> None:
    decision = evaluate_swing_state(
        current,
        previous=previous,
        previous_state=watch_state,
        config=strategy_config,
    )

    assert decision.state is watch_state


def test_strong_opposite_trend_cannot_be_bypassed_by_one_candle_pattern(strategy_config) -> None:
    previous = _feature(
        0,
        close=90.0,
        position=0.10,
        drawdown=-0.30,
        rsi=25.0,
        bb=0.10,
        patterns=("hammer",),
    )
    current = _feature(
        1,
        close=92.0,
        position=0.12,
        drawdown=-0.29,
        rsi=25.0,
        bb=0.15,
        trend="down",
    )

    decision = evaluate_swing_state(
        current,
        previous=previous,
        previous_state=SwingState.LOW_WATCH,
        config=strategy_config,
    )

    assert decision.state is SwingState.LOW_WATCH


def test_state_machine_requires_enough_history_and_strict_date_order(strategy_config) -> None:
    insufficient = _feature(0, bars_available=119)
    assert evaluate_swing_state(insufficient, config=strategy_config).state is SwingState.DATA_INSUFFICIENT

    machine = SwingStateMachine(strategy_config)
    machine.update(_feature(0))
    with pytest.raises(ValueError, match="严格递增"):
        machine.update(_feature(0))


def test_explanation_cannot_override_history_contract_with_explicit_enough_history(strategy_config) -> None:
    low = _feature(
        0,
        position=0.10,
        drawdown=-0.30,
        rsi=25.0,
        bb=0.10,
        patterns=("hammer",),
        bars_available=119,
    )
    decision = evaluate_swing_state(low, config=strategy_config)

    explanation = build_analysis_explanation(
        low,
        decision=decision,
        config=strategy_config,
        enough_history=True,
    )

    assert decision.state is SwingState.DATA_INSUFFICIENT
    assert explanation["low_watch"]["evaluated"] is False
    assert explanation["high_watch"]["evaluated"] is False
    assert explanation["low_watch"]["conditions"] == []
    assert explanation["conclusion"]["state"] == SwingState.DATA_INSUFFICIENT.value


def test_state_machine_series_resets_between_runs(strategy_config) -> None:
    machine = SwingStateMachine(strategy_config)
    first = machine.evaluate([_feature(0), _feature(1)])
    second = machine.evaluate([_feature(0), _feature(1)])

    assert first == second


def test_analysis_explanation_exposes_three_conditions_and_single_threshold_source(strategy_config) -> None:
    low = _feature(
        0,
        close=90.0,
        position=0.10,
        drawdown=-0.30,
        rsi=25.0,
        bb=0.10,
        patterns=("hammer",),
    )
    low_decision = evaluate_swing_state(low, config=strategy_config)
    low_explanation = build_analysis_explanation(
        low,
        decision=low_decision,
        config=strategy_config,
        enough_history=True,
    )
    assert low_explanation["low_watch"]["pass"] is True
    assert len(low_explanation["low_watch"]["conditions"]) == 3
    assert low_explanation["low_watch"]["conditions"][2]["threshold"]["bollinger_percent_b"]["value"] == LOW_BOLLINGER_PERCENT_B_THRESHOLD

    high = _feature(
        1,
        close=110.0,
        position=0.90,
        drawdown=-0.01,
        rsi=75.0,
        bb=0.90,
        patterns=("shooting_star",),
    )
    high_decision = evaluate_swing_state(high, config=strategy_config)
    high_explanation = build_analysis_explanation(
        high,
        decision=high_decision,
        config=strategy_config,
        enough_history=True,
    )
    assert high_explanation["high_watch"]["pass"] is True
    assert len(high_explanation["high_watch"]["conditions"]) == 3
    assert high_explanation["high_watch"]["conditions"][2]["threshold"]["bollinger_percent_b"]["value"] == HIGH_BOLLINGER_PERCENT_B_THRESHOLD


def test_neutral_explanation_says_why_not_low_or_high_and_handles_missing_indicators(strategy_config) -> None:
    neutral = _feature(0, position=0.5, drawdown=-0.03)
    decision = evaluate_swing_state(neutral, config=strategy_config)
    explanation = build_analysis_explanation(
        neutral,
        decision=decision,
        config=strategy_config,
        enough_history=True,
    )
    conclusion = explanation["conclusion"]
    assert decision.state is SwingState.NEUTRAL
    assert "低位" in conclusion["why_not_low"]
    assert "高位" in conclusion["why_not_high"]
    missing = _feature(1, position=0.5, drawdown=-0.03, rsi=None, bb=None)
    missing_decision = evaluate_swing_state(missing, config=strategy_config)
    missing_explanation = build_analysis_explanation(
        missing,
        decision=missing_decision,
        config=strategy_config,
        enough_history=False,
    )
    assert missing_explanation["low_watch"]["evaluated"] is False
    assert missing_explanation["high_watch"]["evaluated"] is False
    assert missing_explanation["low_watch"]["conditions"] == []
    assert "有效收盘日线不足" in missing_explanation["conclusion"]["why"]
    assert explanation["model_boundary"]["strict_invalidation_rule"]


def test_confirmation_explanation_contains_numeric_rsi_chain_and_localised_patterns(strategy_config) -> None:
    previous = _feature(
        0,
        close=90.0,
        position=0.10,
        drawdown=-0.30,
        rsi=25.0,
        bb=0.10,
        patterns=("hammer",),
    )
    current = _feature(
        1,
        close=92.0,
        position=0.12,
        drawdown=-0.29,
        rsi=27.0,
        bb=0.15,
    )
    watch = evaluate_swing_state(previous, config=strategy_config)
    decision = evaluate_swing_state(
        current,
        previous=previous,
        previous_state=watch.state,
        config=strategy_config,
    )
    explanation = build_analysis_explanation(
        current,
        decision=decision,
        previous=previous,
        previous_state=watch.state,
        config=strategy_config,
        enough_history=True,
    )

    route = explanation["confirmation_path"]["bottom"]["routes"][1]
    chain = route["actual"]["rsi_chain"]
    assert chain["current_rsi"] == 27.0
    assert chain["previous_rsi"] == 25.0
    assert chain["required_current_rsi"] == 26.0
    assert "当前RSI 27.0 > 前一日RSI 25.0 + 1.0 = 26.0" in chain["comparison"]
    assert route["threshold"]["rsi"]["formula"] == "当前RSI > 前一日RSI + 1.0"
    assert explanation["confirmation_path"]["bottom"]["routes"][0]["actual"]["previous_patterns"] == ["锤子线"]
    assert "hammer" not in repr(explanation)

    high_previous = _feature(
        2,
        close=110.0,
        position=0.90,
        drawdown=-0.01,
        rsi=75.0,
        bb=0.90,
        patterns=("shooting_star",),
    )
    high_current = _feature(
        3,
        close=108.0,
        position=0.88,
        drawdown=-0.02,
        rsi=73.0,
        bb=0.85,
    )
    high_watch = evaluate_swing_state(high_previous, config=strategy_config)
    high_decision = evaluate_swing_state(
        high_current,
        previous=high_previous,
        previous_state=high_watch.state,
        config=strategy_config,
    )
    high_explanation = build_analysis_explanation(
        high_current,
        decision=high_decision,
        previous=high_previous,
        previous_state=high_watch.state,
        config=strategy_config,
        enough_history=True,
    )
    high_route = high_explanation["confirmation_path"]["top"]["routes"][1]
    high_chain = high_route["actual"]["rsi_chain"]
    assert high_chain["required_current_rsi"] == 74.0
    assert "当前RSI 73.0 < 前一日RSI 75.0 - 1.0 = 74.0" in high_chain["comparison"]
    assert high_route["threshold"]["rsi"]["formula"] == "当前RSI < 前一日RSI - 1.0"
    assert high_explanation["confirmation_path"]["top"]["routes"][0]["actual"]["previous_patterns"] == ["射击之星形态"]


def test_insufficient_explanation_short_circuits_directional_conditions(strategy_config) -> None:
    partial = _feature(
        0,
        position=0.1,
        drawdown=-0.3,
        rsi=25.0,
        bb=0.1,
        bars_available=119,
    )
    decision = evaluate_swing_state(partial, config=strategy_config)
    explanation = build_analysis_explanation(
        partial,
        decision=decision,
        config=strategy_config,
        enough_history=False,
    )

    assert explanation["current_state"]["value"] == SwingState.DATA_INSUFFICIENT.value
    assert explanation["low_watch"]["evaluated"] is False
    assert explanation["high_watch"]["evaluated"] is False
    assert explanation["confirmation_path"]["evaluated"] is False
    assert "低位条件已满足" not in repr(explanation)
