"""中低频持仓机动仓分析的领域模型。

这个模块故意只描述「观察状态」和历史回放所需的数据，不定义下单接口。
策略使用已收盘的日线；任何外部数据源都必须在进入本模块前完成数据清洗和
盘中 K 线剔除。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum


class AssetType(str, Enum):
    """标的类别。ETF 的交易制度由调用者按具体产品传入。"""

    STOCK = "stock"
    ETF = "etf"


class SwingState(str, Enum):
    """给人看的观察状态，不是交易指令。"""

    DATA_INSUFFICIENT = "DATA_INSUFFICIENT"
    LOW_WATCH = "LOW_WATCH"
    BOTTOM_CONFIRMED = "BOTTOM_CONFIRMED"
    NEUTRAL = "NEUTRAL"
    HIGH_WATCH = "HIGH_WATCH"
    TOP_CONFIRMED = "TOP_CONFIRMED"


@dataclass(frozen=True, slots=True)
class SwingConfig:
    """默认的中低频日线参数。

    窗口以交易日计。参数刻意保持为少量、可解释的宽窗口，适合每周或每个
    交易日收盘后观察，不适合分钟级或高频交易。
    """

    price_position_window: int = 120
    trend_fast_window: int = 20
    trend_slow_window: int = 60
    rsi_window: int = 14
    atr_window: int = 14
    bollinger_window: int = 20
    relative_volume_window: int = 20
    min_history_bars: int = 120

    # 低位/高位只用于建立观察区，不单凭一个阈值确认拐点。
    low_position_threshold: float = 0.25
    high_position_threshold: float = 0.75
    low_drawdown_threshold: float = -0.10
    high_drawdown_threshold: float = -0.05
    low_rsi_threshold: float = 42.0
    high_rsi_threshold: float = 58.0
    reversal_rsi_tolerance: float = 1.0

    # 机动仓参数只用于历史模拟，不代表真实配置或投资建议。
    core_weight: float = 0.80
    tactical_weight: float = 0.20
    min_holding_bars: int = 10
    action_cooldown_bars: int = 10

    def __post_init__(self) -> None:
        positive_windows = (
            self.price_position_window,
            self.trend_fast_window,
            self.trend_slow_window,
            self.rsi_window,
            self.atr_window,
            self.bollinger_window,
            self.relative_volume_window,
            self.min_history_bars,
        )
        if any(value < 2 for value in positive_windows):
            raise ValueError("指标窗口和最小历史长度必须至少为 2 个交易日")
        if self.trend_fast_window >= self.trend_slow_window:
            raise ValueError("短均线窗口必须小于长均线窗口")
        if self.core_weight < 0 or self.tactical_weight < 0:
            raise ValueError("核心仓和机动仓权重不能为负")
        float_parameters = (
            self.low_position_threshold,
            self.high_position_threshold,
            self.low_drawdown_threshold,
            self.high_drawdown_threshold,
            self.low_rsi_threshold,
            self.high_rsi_threshold,
            self.reversal_rsi_tolerance,
            self.core_weight,
            self.tactical_weight,
        )
        if not all(math.isfinite(value) for value in float_parameters):
            raise ValueError("策略阈值和仓位权重必须是有限数值")
        if abs(self.core_weight + self.tactical_weight - 1.0) > 1e-9:
            raise ValueError("核心仓和机动仓权重之和必须为 1")
        if self.min_holding_bars < 0 or self.action_cooldown_bars < 0:
            raise ValueError("持有期和冷静期不能为负")
        if not 0 <= self.low_position_threshold < self.high_position_threshold <= 1:
            raise ValueError("高低位价格区间阈值必须在 0 到 1 之间且低位小于高位")
        if not 0 <= self.low_rsi_threshold < self.high_rsi_threshold <= 100:
            raise ValueError("RSI 高低位阈值必须在 0 到 100 之间且低位小于高位")
        if self.reversal_rsi_tolerance < 0:
            raise ValueError("RSI 反转容差不能为负")
        if not self.low_drawdown_threshold < self.high_drawdown_threshold <= 0:
            raise ValueError("回撤阈值必须为负数或零且低位阈值小于高位阈值")


@dataclass(frozen=True, slots=True)
class TransactionCosts:
    """历史模拟成本，以基点表示，按具体标的由调用者传入。

    ``commission_bps`` 和 ``slippage_bps`` 双向适用；``sell_tax_bps`` 只在
    机动仓减少时适用。默认值为零，避免把某一家券商费率冒充通用事实。
    """

    commission_bps: float = 0.0
    slippage_bps: float = 0.0
    sell_tax_bps: float = 0.0

    def __post_init__(self) -> None:
        values = (self.commission_bps, self.slippage_bps, self.sell_tax_bps)
        if not all(math.isfinite(value) and value >= 0 for value in values):
            raise ValueError("手续费、滑点和卖出税费不能为负")

    def rate(self, *, increasing_exposure: bool) -> float:
        """返回本次机动仓调整的成本比例。"""

        rate_bps = self.commission_bps + self.slippage_bps
        if not increasing_exposure:
            rate_bps += self.sell_tax_bps
        return rate_bps / 10_000


@dataclass(frozen=True, slots=True)
class SwingFeatures:
    """某个已收盘交易日可观测的指标快照。"""

    trade_date: datetime
    close_price: float
    open_price: float
    high_price: float
    low_price: float
    volume: float
    price_position: float | None = None
    drawdown: float | None = None
    ma_fast: float | None = None
    ma_slow: float | None = None
    ma_fast_slope: float | None = None
    rsi: float | None = None
    atr: float | None = None
    atr_pct: float | None = None
    bollinger_percent_b: float | None = None
    bollinger_bandwidth: float | None = None
    relative_volume: float | None = None
    candle_patterns: tuple[str, ...] = field(default_factory=tuple)
    trend_regime: str = "unknown"
    bars_available: int = 0

    @property
    def date(self) -> date:
        return self.trade_date.date()

    @property
    def is_complete(self) -> bool:
        """指标是否已具备最小历史数据。"""

        return self.bars_available > 0 and all(
            value is not None
            for value in (
                self.price_position,
                self.drawdown,
                self.ma_fast,
                self.ma_slow,
                self.rsi,
                self.atr,
                self.bollinger_percent_b,
                self.relative_volume,
            )
        )


@dataclass(frozen=True, slots=True)
class SwingDecision:
    """状态机在收盘时生成的观察结论。

    ``tactical_target`` 是历史模拟中的机动仓目标暴露（0 到 1，代表机动仓
    内部的现金/标的比例），不是订单、买卖建议或券商指令； ``None`` 表示
    维持上一次模拟暴露。
    """

    trade_date: datetime
    state: SwingState
    features: SwingFeatures
    confidence: str = "low"
    reasons: tuple[str, ...] = field(default_factory=tuple)
    tactical_target: float | None = None

    @property
    def is_observation_only(self) -> bool:
        return True


@dataclass(frozen=True, slots=True)
class TradeEvent:
    """历史模拟中机动仓暴露变化的记录，不是实际成交回报。"""

    signal_date: datetime
    execution_date: datetime
    from_target: float
    to_target: float
    execution_price: float
    gross_notional: float
    cost: float
    reason: str


@dataclass(frozen=True, slots=True)
class EquityPoint:
    """同一日期的两条历史净值曲线。"""

    trade_date: datetime
    buy_and_hold_value: float
    core_tactical_value: float
    tactical_target: float
    # 静态核心仓/现金基准：核心仓始终持有，机动仓始终留在现金。
    # 放在已有字段之后并给默认值，兼容外部按旧字段构造 EquityPoint 的调用者。
    static_core_cash_value: float = 0.0


@dataclass(frozen=True, slots=True)
class PerformanceMetrics:
    """一段历史回放的可核验统计。"""

    total_return: float
    max_drawdown: float
    trade_count: int
    turnover: float
    final_value: float
    # 以下指标均按日线观测、252 个交易日年化；短样本不足时返回有限的
    # 0.0，而不是抛异常或生成无穷值，便于网页和报告层稳定展示。
    annualized_return: float = 0.0
    annualized_volatility: float = 0.0
    calmar_ratio: float = 0.0
    market_exposure: float = 1.0


@dataclass(frozen=True, slots=True)
class BacktestResult:
    """buy-and-hold 与核心仓/机动仓历史回放对比。"""

    start_date: datetime
    end_date: datetime
    initial_capital: float
    buy_and_hold: PerformanceMetrics
    core_tactical: PerformanceMetrics
    decisions: tuple[SwingDecision, ...] = field(default_factory=tuple)
    trades: tuple[TradeEvent, ...] = field(default_factory=tuple)
    equity_curve: tuple[EquityPoint, ...] = field(default_factory=tuple)
    # 与动态核心/机动回放同一评估区间的静态基准。None 只为兼容旧版外部
    # 构造调用；run_backtest 始终填充该字段。
    static_core_cash: PerformanceMetrics | None = None
    # 因下一根日线无成交量或无成交额而顺延的待执行信号次数。
    deferred_count: int = 0
