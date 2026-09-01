"""固定、透明的低频波段模型敏感性检查。

本模块只做历史回放的切分、轻微参数扰动和成本压力检查。它不搜索参数、
不自动选择赢家，也不把任何一个结果称为策略证明。输出只包含回测统计，
不会携带证券代码、名称或持仓信息。
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime

from trading_agent.domain.models import DailyBar
from trading_agent.swing.backtest import run_backtest
from trading_agent.swing.models import (
    AssetType,
    BacktestResult,
    EquityPoint,
    SwingConfig,
    TradeEvent,
    TransactionCosts,
)

STATUS_OK = "ok"
STATUS_DATA_INSUFFICIENT = "data_insufficient"
_MIN_PHASE_POINTS = 20
_PHASE_COUNT = 3
_DIRECTION_TOLERANCE = 0.001  # 10 bp
_PERTURBATION_FACTOR = 0.20
_COST_MULTIPLIERS = (2.0, 3.0)


@dataclass(frozen=True, slots=True)
class ScenarioResult:
    """一个固定检查情景的匿名统计结果。

    ``status`` 为 ``data_insufficient`` 时，绩效字段全部为 ``None``，避免
    用不完整样本编出数字。``scenario`` 只使用通用情景名，不包含标的信息。
    """

    scenario: str
    status: str = STATUS_OK
    dynamic_return: float | None = None
    buy_hold_return: float | None = None
    static_return: float | None = None
    excess_vs_buy_hold: float | None = None
    excess_vs_static: float | None = None
    dynamic_max_drawdown: float | None = None
    trade_count: int | None = None
    observation_count: int | None = None
    start_date: datetime | None = None
    end_date: datetime | None = None
    parameter_profile: str | None = None
    cost_multiplier: float | None = None
    reason: str | None = None

    def as_dict(self) -> dict[str, object]:
        """转换为可直接写入本机 JSON 的匿名结构。"""

        return {
            "scenario": self.scenario,
            "status": self.status,
            "dynamic_return": self.dynamic_return,
            "buy_hold_return": self.buy_hold_return,
            "static_return": self.static_return,
            "excess_vs_buy_hold": self.excess_vs_buy_hold,
            "excess_vs_static": self.excess_vs_static,
            "dynamic_max_drawdown": self.dynamic_max_drawdown,
            "trade_count": self.trade_count,
            "observation_count": self.observation_count,
            "start_date": self.start_date.isoformat() if self.start_date else None,
            "end_date": self.end_date.isoformat() if self.end_date else None,
            "parameter_profile": self.parameter_profile,
            "cost_multiplier": self.cost_multiplier,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class RobustnessSummary:
    """一组预先固定的敏感性检查结果。

    ``scenarios`` 按固定顺序包含 3 个连续阶段、2 个参数扰动和 2 个成本
    压力情景。``direction_consistent_count`` / ``direction_total`` 只针对
    四个扰动/成本情景，不针对三段时间切分；方向判断使用 10 bp 容差。
    """

    status: str
    baseline: ScenarioResult
    scenarios: tuple[ScenarioResult, ...] = ()
    direction_consistent_count: int | None = None
    direction_total: int | None = None
    direction_tolerance: float = _DIRECTION_TOLERANCE

    @property
    def direction_consistent_ratio(self) -> float | None:
        """返回方向一致比例；数据不足时保持为空。"""

        if self.direction_consistent_count is None or self.direction_total in (None, 0):
            return None
        return self.direction_consistent_count / self.direction_total

    @property
    def phase_scenarios(self) -> tuple[ScenarioResult, ...]:
        """返回三段历史阶段，方便报告层按类型展示。"""

        return tuple(item for item in self.scenarios if item.scenario.startswith("phase_"))

    @property
    def parameter_scenarios(self) -> tuple[ScenarioResult, ...]:
        """返回固定的收紧/放宽参数情景。"""

        return tuple(item for item in self.scenarios if item.parameter_profile is not None)

    @property
    def cost_scenarios(self) -> tuple[ScenarioResult, ...]:
        """返回固定的 2x/3x 成本情景。"""

        return tuple(item for item in self.scenarios if item.cost_multiplier is not None)

    def as_dict(self) -> dict[str, object]:
        """转换为报告和网页可消费的匿名 JSON 结构。"""

        return {
            "status": self.status,
            "baseline": self.baseline.as_dict(),
            "scenarios": [item.as_dict() for item in self.scenarios],
            "direction_consistent_count": self.direction_consistent_count,
            "direction_total": self.direction_total,
            "direction_consistent_ratio": self.direction_consistent_ratio,
            "direction_tolerance": self.direction_tolerance,
            "interpretation": "sensitivity_check_not_strategy_proof",
        }


def _empty_scenario(
    name: str,
    *,
    parameter_profile: str | None = None,
    cost_multiplier: float | None = None,
    reason: str = "baseline_equity_curve_insufficient",
) -> ScenarioResult:
    return ScenarioResult(
        scenario=name,
        status=STATUS_DATA_INSUFFICIENT,
        parameter_profile=parameter_profile,
        cost_multiplier=cost_multiplier,
        reason=reason,
    )


def _is_valid_curve(curve: Sequence[EquityPoint]) -> bool:
    """检查净值曲线是否可用于比较，不暴露曲线内容。"""

    if not curve:
        return False
    for point in curve:
        values = (
            point.core_tactical_value,
            point.buy_and_hold_value,
            point.static_core_cash_value,
        )
        if not all(
            isinstance(value, (int, float)) and math.isfinite(float(value)) and float(value) > 0
            for value in values
        ):
            return False
        if getattr(point, "trade_date", None) is None:
            return False
    return True


def _max_drawdown(values: Sequence[float]) -> float:
    peak = values[0]
    drawdown = 0.0
    for value in values:
        peak = max(peak, value)
        if peak > 0:
            drawdown = min(drawdown, value / peak - 1.0)
    return drawdown


def _scenario_from_metrics(
    name: str,
    result: BacktestResult,
    *,
    parameter_profile: str | None = None,
    cost_multiplier: float | None = None,
) -> ScenarioResult:
    """从已完成回放提取匿名、可比较的统计。"""

    static = result.static_core_cash
    if static is None:
        return _empty_scenario(
            name,
            parameter_profile=parameter_profile,
            cost_multiplier=cost_multiplier,
            reason="static_baseline_unavailable",
        )
    dynamic_return = result.core_tactical.total_return
    buy_hold_return = result.buy_and_hold.total_return
    static_return = static.total_return
    return ScenarioResult(
        scenario=name,
        dynamic_return=dynamic_return,
        buy_hold_return=buy_hold_return,
        static_return=static_return,
        excess_vs_buy_hold=dynamic_return - buy_hold_return,
        excess_vs_static=dynamic_return - static_return,
        dynamic_max_drawdown=result.core_tactical.max_drawdown,
        trade_count=result.core_tactical.trade_count,
        observation_count=len(result.equity_curve),
        start_date=result.start_date,
        end_date=result.end_date,
        parameter_profile=parameter_profile,
        cost_multiplier=cost_multiplier,
    )


def _phase_result(
    phase_number: int,
    points: Sequence[EquityPoint],
    trades: Sequence[TradeEvent],
) -> ScenarioResult:
    """对一段不重叠的净值点计算三条曲线的区间统计。"""

    first = points[0]
    last = points[-1]
    dynamic_values = [float(point.core_tactical_value) for point in points]
    buy_hold_values = [float(point.buy_and_hold_value) for point in points]
    static_values = [float(point.static_core_cash_value) for point in points]
    dynamic_return = dynamic_values[-1] / dynamic_values[0] - 1.0
    buy_hold_return = buy_hold_values[-1] / buy_hold_values[0] - 1.0
    static_return = static_values[-1] / static_values[0] - 1.0
    start_date = first.trade_date
    end_date = last.trade_date
    trade_count = sum(start_date <= trade.execution_date <= end_date for trade in trades)
    return ScenarioResult(
        scenario=f"phase_{phase_number}",
        dynamic_return=dynamic_return,
        buy_hold_return=buy_hold_return,
        static_return=static_return,
        excess_vs_buy_hold=dynamic_return - buy_hold_return,
        excess_vs_static=dynamic_return - static_return,
        dynamic_max_drawdown=_max_drawdown(dynamic_values),
        trade_count=trade_count,
        observation_count=len(points),
        start_date=start_date,
        end_date=end_date,
    )


def _split_phases(
    result: BacktestResult,
) -> tuple[tuple[EquityPoint, ...], tuple[EquityPoint, ...], tuple[EquityPoint, ...]] | None:
    """等分成三个连续、不重叠阶段；每段至少包含 20 个净值点。"""

    points = tuple(result.equity_curve)
    if len(points) < _PHASE_COUNT * _MIN_PHASE_POINTS or not _is_valid_curve(points):
        return None
    quotient, remainder = divmod(len(points), _PHASE_COUNT)
    lengths = tuple(quotient + (1 if index < remainder else 0) for index in range(_PHASE_COUNT))
    if min(lengths) < _MIN_PHASE_POINTS:
        return None
    phases: list[tuple[EquityPoint, ...]] = []
    cursor = 0
    for length in lengths:
        phases.append(points[cursor : cursor + length])
        cursor += length
    return phases[0], phases[1], phases[2]


def _pair_thresholds(
    low: float,
    high: float,
    *,
    lower_bound: float,
    upper_bound: float,
    widen: bool,
) -> tuple[float, float]:
    """按约 20% 改变阈值间距，并在合法边界内保持顺序。"""

    center = (low + high) / 2.0
    half_gap = (high - low) / 2.0
    factor = 1.0 + _PERTURBATION_FACTOR if widen else 1.0 - _PERTURBATION_FACTOR
    half_gap *= factor
    adjusted_low = max(lower_bound, center - half_gap)
    adjusted_high = min(upper_bound, center + half_gap)
    if adjusted_low >= adjusted_high:
        # 只有极端自定义配置才会走到这里；保留一个最小正间距，确保
        # SwingConfig 的合法性校验仍然生效且不改变其它窗口参数。
        gap = max((upper_bound - lower_bound) * 1e-6, 1e-9)
        midpoint = min(max(center, lower_bound + gap), upper_bound - gap)
        adjusted_low = midpoint - gap / 2.0
        adjusted_high = midpoint + gap / 2.0
    return adjusted_low, adjusted_high


def _perturbed_config(base: SwingConfig, *, tighten: bool) -> SwingConfig:
    """生成固定的收紧/放宽版本，不修改传入的 frozen config。"""

    position_low, position_high = _pair_thresholds(
        base.low_position_threshold,
        base.high_position_threshold,
        lower_bound=0.0,
        upper_bound=1.0,
        widen=tighten,
    )
    rsi_low, rsi_high = _pair_thresholds(
        base.low_rsi_threshold,
        base.high_rsi_threshold,
        lower_bound=0.0,
        upper_bound=100.0,
        widen=tighten,
    )
    drawdown_low, drawdown_high = _pair_thresholds(
        base.low_drawdown_threshold,
        base.high_drawdown_threshold,
        lower_bound=-1.0,
        upper_bound=0.0,
        widen=tighten,
    )
    tolerance_factor = 1.0 + _PERTURBATION_FACTOR if tighten else 1.0 - _PERTURBATION_FACTOR
    tolerance = max(0.0, base.reversal_rsi_tolerance * tolerance_factor)
    # 构造函数会再次检查所有约束；这里显式保留它作为“合法配置”的最终门。
    return replace(
        base,
        low_position_threshold=position_low,
        high_position_threshold=position_high,
        low_drawdown_threshold=drawdown_low,
        high_drawdown_threshold=drawdown_high,
        low_rsi_threshold=rsi_low,
        high_rsi_threshold=rsi_high,
        reversal_rsi_tolerance=tolerance,
    )


def _scaled_costs(base: TransactionCosts, multiplier: float) -> TransactionCosts:
    return TransactionCosts(
        commission_bps=base.commission_bps * multiplier,
        slippage_bps=base.slippage_bps * multiplier,
        sell_tax_bps=base.sell_tax_bps * multiplier,
    )


def _run_scenario(
    bars: Sequence[DailyBar],
    *,
    asset_type: AssetType | str,
    costs: TransactionCosts,
    config: SwingConfig,
    initial_capital: float,
    scenario: str,
    parameter_profile: str | None = None,
    cost_multiplier: float | None = None,
) -> ScenarioResult:
    try:
        result = run_backtest(
            bars,
            asset_type=asset_type,
            costs=costs,
            config=config,
            initial_capital=initial_capital,
        )
    except (ValueError, TypeError, OverflowError):
        return _empty_scenario(
            scenario,
            parameter_profile=parameter_profile,
            cost_multiplier=cost_multiplier,
            reason="backtest_unavailable",
        )
    return _scenario_from_metrics(
        scenario,
        result,
        parameter_profile=parameter_profile,
        cost_multiplier=cost_multiplier,
    )


def run_robustness_checks(
    bars: Iterable[DailyBar],
    *,
    asset_type: AssetType | str = AssetType.STOCK,
    costs: TransactionCosts | None = None,
    config: SwingConfig | None = None,
    initial_capital: float = 1.0,
    base_result: BacktestResult | None = None,
) -> RobustnessSummary:
    """运行固定的三段、参数和成本敏感性检查。

    ``base_result`` 可由调用方预先传入，避免重复执行基础回放；传入时仍需
    提供同一批 ``bars``，因为参数和成本情景必须使用相同的历史区间。
    所有情景沿用 ``run_backtest`` 的预热排除、t+1 开盘执行和资产类型参数。
    """

    settings = config or SwingConfig()
    fee_model = costs or TransactionCosts()
    bar_list = tuple(bars)

    if base_result is None:
        try:
            base_result = run_backtest(
                bar_list,
                asset_type=asset_type,
                costs=fee_model,
                config=settings,
                initial_capital=initial_capital,
            )
        except (ValueError, TypeError, OverflowError):
            baseline_scenario = _empty_scenario("baseline")
            unavailable = tuple(
                [
                    _empty_scenario(f"phase_{index}")
                    for index in range(1, _PHASE_COUNT + 1)
                ]
                + [
                    _empty_scenario(
                        "parameter_tighten_20pct",
                        parameter_profile="tighten_20pct",
                    ),
                    _empty_scenario(
                        "parameter_loosen_20pct",
                        parameter_profile="loosen_20pct",
                    ),
                    _empty_scenario("cost_2x", cost_multiplier=2.0),
                    _empty_scenario("cost_3x", cost_multiplier=3.0),
                ]
            )
            return RobustnessSummary(
                status=STATUS_DATA_INSUFFICIENT,
                baseline=baseline_scenario,
                scenarios=unavailable,
            )

    phases = _split_phases(base_result)
    baseline_scenario = _scenario_from_metrics("baseline", base_result)
    if phases is None or baseline_scenario.status != STATUS_OK:
        unavailable = tuple(
            [
                _empty_scenario(f"phase_{index}")
                for index in range(1, _PHASE_COUNT + 1)
            ]
            + [
                _empty_scenario(
                    "parameter_tighten_20pct",
                    parameter_profile="tighten_20pct",
                ),
                _empty_scenario(
                    "parameter_loosen_20pct",
                    parameter_profile="loosen_20pct",
                ),
                _empty_scenario("cost_2x", cost_multiplier=2.0),
                _empty_scenario("cost_3x", cost_multiplier=3.0),
            ]
        )
        return RobustnessSummary(
            status=STATUS_DATA_INSUFFICIENT,
            baseline=_empty_scenario("baseline"),
            scenarios=unavailable,
        )

    phase_scenarios = tuple(
        _phase_result(index, phase, base_result.trades)
        for index, phase in enumerate(phases, start=1)
    )
    parameter_scenarios = (
        _run_scenario(
            bar_list,
            asset_type=asset_type,
            costs=fee_model,
            config=_perturbed_config(settings, tighten=True),
            initial_capital=initial_capital,
            scenario="parameter_tighten_20pct",
            parameter_profile="tighten_20pct",
        ),
        _run_scenario(
            bar_list,
            asset_type=asset_type,
            costs=fee_model,
            config=_perturbed_config(settings, tighten=False),
            initial_capital=initial_capital,
            scenario="parameter_loosen_20pct",
            parameter_profile="loosen_20pct",
        ),
    )
    cost_scenarios = tuple(
        _run_scenario(
            bar_list,
            asset_type=asset_type,
            costs=_scaled_costs(fee_model, multiplier),
            config=settings,
            initial_capital=initial_capital,
            scenario=f"cost_{int(multiplier)}x",
            cost_multiplier=multiplier,
        )
        for multiplier in _COST_MULTIPLIERS
    )
    scenarios = phase_scenarios + parameter_scenarios + cost_scenarios

    base_excess = baseline_scenario.excess_vs_buy_hold
    direction_count: int | None = None
    direction_total: int | None = None
    if base_excess is not None and all(item.status == STATUS_OK for item in parameter_scenarios + cost_scenarios):
        base_direction = 1 if base_excess > _DIRECTION_TOLERANCE else -1 if base_excess < -_DIRECTION_TOLERANCE else 0
        direction_count = sum(
            (
                1
                if (
                    1
                    if item.excess_vs_buy_hold is not None
                    and item.excess_vs_buy_hold > _DIRECTION_TOLERANCE
                    else -1
                    if item.excess_vs_buy_hold is not None
                    and item.excess_vs_buy_hold < -_DIRECTION_TOLERANCE
                    else 0
                )
                == base_direction
                else 0
            )
            for item in parameter_scenarios + cost_scenarios
        )
        direction_total = len(parameter_scenarios) + len(cost_scenarios)

    status = (
        STATUS_OK
        if direction_total is not None
        else STATUS_DATA_INSUFFICIENT
    )
    return RobustnessSummary(
        status=status,
        baseline=baseline_scenario,
        scenarios=scenarios,
        direction_consistent_count=direction_count,
        direction_total=direction_total,
    )


__all__ = ["RobustnessSummary", "ScenarioResult", "run_robustness_checks"]
