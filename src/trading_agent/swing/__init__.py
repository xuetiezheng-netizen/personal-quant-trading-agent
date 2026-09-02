"""现有持仓机动仓的中低频日线波段研究模块。"""

from trading_agent.swing.backtest import backtest, run_backtest
from trading_agent.swing.features import (
    calculate_swing_features,
    compute_features,
    latest_features,
    prepare_bars,
)
from trading_agent.swing.models import (
    AssetType,
    BacktestResult,
    EquityPoint,
    PerformanceMetrics,
    SwingConfig,
    SwingDecision,
    SwingFeatures,
    SwingState,
    TradeEvent,
    TransactionCosts,
)
from trading_agent.swing.strategy import (
    HIGH_BOLLINGER_PERCENT_B_THRESHOLD,
    LOW_BOLLINGER_PERCENT_B_THRESHOLD,
    SwingStateMachine,
    build_analysis_explanation,
    evaluate_swing_series,
    evaluate_swing_state,
)

__all__ = [
    "HIGH_BOLLINGER_PERCENT_B_THRESHOLD",
    "LOW_BOLLINGER_PERCENT_B_THRESHOLD",
    "AssetType",
    "BacktestResult",
    "EquityPoint",
    "PerformanceMetrics",
    "SwingConfig",
    "SwingDecision",
    "SwingFeatures",
    "SwingState",
    "SwingStateMachine",
    "TradeEvent",
    "TransactionCosts",
    "backtest",
    "build_analysis_explanation",
    "calculate_swing_features",
    "compute_features",
    "evaluate_swing_series",
    "evaluate_swing_state",
    "latest_features",
    "prepare_bars",
    "run_backtest",
]
