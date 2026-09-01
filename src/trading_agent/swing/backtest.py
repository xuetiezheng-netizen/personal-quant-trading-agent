"""核心仓/机动仓的低换手历史回放。

回放严格使用「t 日收盘产生状态，t+1 日开盘成交」的时序。它是研究工具，
不会连接券商、不会发送订单，也不会把回测结果解释为未来收益承诺。
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass

from trading_agent.domain.models import DailyBar
from trading_agent.swing.features import calculate_swing_features, prepare_bars
from trading_agent.swing.models import (
    AssetType,
    BacktestResult,
    EquityPoint,
    PerformanceMetrics,
    SwingConfig,
    SwingDecision,
    TradeEvent,
    TransactionCosts,
)
from trading_agent.swing.strategy import SwingStateMachine


@dataclass(slots=True)
class _PortfolioState:
    core_units: float
    tactical_units: float
    tactical_cash: float
    tactical_target: float
    tactical_held_since: int | None
    last_action_index: int | None
    turnover: float = 0.0
    costs: float = 0.0
    trades: list[TradeEvent] | None = None


def _metrics(values: list[float], *, trade_count: int, turnover: float) -> PerformanceMetrics:
    if not values:
        raise ValueError("净值序列不能为空")
    initial = values[0]
    peak = initial
    max_drawdown = 0.0
    for value in values:
        peak = max(peak, value)
        if peak > 0:
            max_drawdown = min(max_drawdown, value / peak - 1.0)
    return PerformanceMetrics(
        total_return=values[-1] / initial - 1.0,
        max_drawdown=max_drawdown,
        trade_count=trade_count,
        turnover=turnover,
        final_value=values[-1],
    )


def _coerce_asset_type(value: AssetType | str) -> AssetType:
    if isinstance(value, AssetType):
        return value
    try:
        return AssetType(value.lower())
    except (AttributeError, ValueError) as exc:
        raise ValueError("asset_type 必须是 'stock' 或 'etf'") from exc


def _execute_target(
    state: _PortfolioState,
    *,
    signal: SwingDecision,
    execution_bar: DailyBar,
    execution_index: int,
    target: float,
    costs: TransactionCosts,
) -> TradeEvent | None:
    """在 t+1 开盘把机动仓调整到目标暴露。"""

    if not 0.0 <= target <= 1.0:
        raise ValueError("机动仓目标暴露必须位于 0 到 1 之间")
    if math.isclose(target, state.tactical_target, abs_tol=1e-12):
        return None

    open_price = execution_bar.open_price
    increasing = target > state.tactical_target
    slippage_rate = costs.slippage_bps / 10_000
    commission_rate = costs.commission_bps / 10_000
    sell_tax_rate = costs.sell_tax_bps / 10_000 if not increasing else 0.0

    if increasing:
        # 当前实现只由状态机产生 0 或 1；这里保留比例逻辑，便于后续做
        # 更保守的部分暴露而不改变时序规则。
        tactical_value_at_open = state.tactical_units * open_price + state.tactical_cash
        target_value = tactical_value_at_open * target
        current_value = state.tactical_units * open_price
        additional_value = max(0.0, target_value - current_value)
        fill_price = open_price * (1.0 + slippage_rate)
        units = additional_value / (fill_price * (1.0 + commission_rate)) if fill_price > 0 else 0.0
        notional = units * fill_price
        commission = notional * commission_rate
        slippage_cost = units * max(0.0, fill_price - open_price)
        state.tactical_units += units
        state.tactical_cash = max(0.0, state.tactical_cash - notional - commission)
        cost = commission + slippage_cost
    else:
        tactical_value_at_open = state.tactical_units * open_price + state.tactical_cash
        target_value = tactical_value_at_open * target
        current_value = state.tactical_units * open_price
        value_to_sell = min(current_value, max(0.0, current_value - target_value))
        units = value_to_sell / open_price if open_price > 0 else 0.0
        fill_price = open_price * (1.0 - slippage_rate)
        notional = units * fill_price
        commission = notional * commission_rate
        sell_tax = notional * sell_tax_rate
        slippage_cost = units * max(0.0, open_price - fill_price)
        state.tactical_units = max(0.0, state.tactical_units - units)
        state.tactical_cash += notional - commission - sell_tax
        cost = commission + sell_tax + slippage_cost

    event = TradeEvent(
        signal_date=signal.trade_date,
        execution_date=execution_bar.trade_date,
        from_target=state.tactical_target,
        to_target=target,
        execution_price=fill_price,
        gross_notional=notional,
        cost=cost,
        reason=signal.state.value,
    )
    state.tactical_target = target
    state.tactical_held_since = execution_index if target > 0 else None
    state.last_action_index = execution_index
    # 换手率按成交前的开盘参考金额统计，避免滑点/手续费改变
    # 「交易规模」本身；成本另外记录在 event.cost 和净值中。
    state.turnover += additional_value if increasing else value_to_sell
    state.costs += cost
    if state.trades is not None:
        state.trades.append(event)
    return event


def _eligible(
    state: _PortfolioState,
    *,
    target: float,
    execution_index: int,
    config: SwingConfig,
) -> bool:
    if math.isclose(target, state.tactical_target, abs_tol=1e-12):
        return False
    if (
        state.last_action_index is not None
        and execution_index - state.last_action_index < config.action_cooldown_bars
    ):
        return False
    # 只有从有仓到降低暴露时需要满足最短持有期；从空仓恢复暴露由冷静期
    # 约束，避免把两个约束误叠加为不可解释的超长等待。
    return not (
        target < state.tactical_target
        and state.tactical_held_since is not None
        and execution_index - state.tactical_held_since < config.min_holding_bars
    )


def run_backtest(
    bars: Iterable[DailyBar],
    *,
    asset_type: AssetType | str = AssetType.STOCK,
    costs: TransactionCosts | None = None,
    config: SwingConfig | None = None,
    initial_capital: float = 1.0,
) -> BacktestResult:
    """比较全仓持有与核心仓/机动仓的历史净值。

    ``asset_type`` 用于显式记录调用者的股票/ETF 选择，费率仍必须通过
    ``costs`` 传入，因为 ETF 的交易制度和费率也可能因产品而不同。
    当前函数不会把股票或 ETF 的制度差异擅自硬编码成交易规则。
    """

    _coerce_asset_type(asset_type)  # 先验证参数，避免把拼写错误静默吞掉。
    settings = config or SwingConfig()
    fee_model = costs or TransactionCosts()
    if initial_capital <= 0 or not math.isfinite(initial_capital):
        raise ValueError("initial_capital 必须是正数")
    prepared = prepare_bars(bars)
    if not prepared:
        raise ValueError("至少需要一根有效日线")
    features = calculate_swing_features(prepared, config=settings)
    decisions = SwingStateMachine(settings).evaluate(features)

    first_close = prepared[0].close_price
    buy_hold_units = initial_capital / first_close
    state = _PortfolioState(
        core_units=initial_capital * settings.core_weight / first_close,
        tactical_units=initial_capital * settings.tactical_weight / first_close,
        tactical_cash=0.0,
        tactical_target=1.0,
        tactical_held_since=0 if settings.tactical_weight > 0 else None,
        last_action_index=None,
        trades=[],
    )
    buy_hold_values: list[float] = []
    core_tactical_values: list[float] = []
    equity_curve: list[EquityPoint] = []

    for index, bar in enumerate(prepared):
        # 决策在上一根 K 收盘才存在，故成交价只能来自当前 bar 的 open。
        if index > 0:
            signal = decisions[index - 1]
            target = signal.tactical_target
            if target is not None and _eligible(
                state,
                target=target,
                execution_index=index,
                config=settings,
            ):
                _execute_target(
                    state,
                    signal=signal,
                    execution_bar=bar,
                    execution_index=index,
                    target=target,
                    costs=fee_model,
                )

        buy_hold_value = buy_hold_units * bar.close_price
        core_tactical_value = state.core_units * bar.close_price + state.tactical_units * bar.close_price + state.tactical_cash
        buy_hold_values.append(buy_hold_value)
        core_tactical_values.append(core_tactical_value)
        equity_curve.append(
            EquityPoint(
                trade_date=bar.trade_date,
                buy_and_hold_value=buy_hold_value,
                core_tactical_value=core_tactical_value,
                tactical_target=state.tactical_target,
            )
        )

    return BacktestResult(
        start_date=prepared[0].trade_date,
        end_date=prepared[-1].trade_date,
        initial_capital=initial_capital,
        buy_and_hold=_metrics(
            buy_hold_values,
            trade_count=0,
            turnover=0.0,
        ),
        core_tactical=_metrics(
            core_tactical_values,
            trade_count=len(state.trades or []),
            turnover=state.turnover / initial_capital,
        ),
        decisions=decisions,
        trades=tuple(state.trades or ()),
        equity_curve=tuple(equity_curve),
    )


def backtest(*args: object, **kwargs: object) -> BacktestResult:
    """``run_backtest`` 的兼容别名。"""

    return run_backtest(*args, **kwargs)  # type: ignore[arg-type]
