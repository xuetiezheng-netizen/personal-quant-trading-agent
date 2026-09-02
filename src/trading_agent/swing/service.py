"""持仓波段观察服务。

这个模块把本机私有持仓、公开日线、确定性特征、状态机和历史回放串在一
起，供本地网页和命令行共用。它刻意不提供下单接口，也不把任意文件路径
交给请求方：持仓、报告和运行摘要只能落在仓库的 ``data/private`` 下。

默认参数面向数周至数月的日线观察。``t`` 日收盘才形成观察状态，历史模拟
在 ``t+1`` 日开盘处理机动仓假设；这不是盘中或高频交易程序。
"""

from __future__ import annotations

import json
import math
import os
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

from trading_agent.domain.models import DailyBar
from trading_agent.swing.backtest import run_backtest
from trading_agent.swing.data import HistoryData
from trading_agent.swing.features import calculate_swing_features, prepare_bars
from trading_agent.swing.history_providers import (
    HistoryFailoverError,
    build_default_history_provider,
)
from trading_agent.swing.models import (
    BacktestResult,
    PerformanceMetrics,
    SwingConfig,
    SwingDecision,
    SwingState,
    TransactionCosts,
)
from trading_agent.swing.portfolio import Holding, PortfolioSnapshot, PortfolioStore
from trading_agent.swing.strategy import SwingStateMachine
from trading_agent.swing.validation import run_robustness_checks

STRATEGY_VERSION = "swing-low-frequency-v1.1"
"""可写入报告的策略版本；修改核心规则时应同步递增。"""

# 回测不能假装交易免费。佣金与滑点是可替换的保守示例值；股票卖出税费
# 使用当前 A 股单边 5 bps 口径，ETF 默认不计证券交易印花税。报告会明确
# 提醒用户：实际佣金、最低收费和产品制度仍应以自己的券商交割单为准。
DEFAULT_COMMISSION_BPS = 3.0
DEFAULT_SLIPPAGE_BPS = 5.0
DEFAULT_STOCK_SELL_TAX_BPS = 5.0
DEFAULT_ETF_SELL_TAX_BPS = 0.0

SHANGHAI = timezone(timedelta(hours=8), name="Asia/Shanghai")
DEFAULT_REPO_ROOT = Path(__file__).resolve().parents[3]
PRIVATE_RELATIVE_PATH = Path("data") / "private"
REPORTS_RELATIVE_PATH = PRIVATE_RELATIVE_PATH / "reports"
LATEST_RESULTS_RELATIVE_PATH = PRIVATE_RELATIVE_PATH / "latest-results.json"
RUN_SUMMARY_RELATIVE_PATH = PRIVATE_RELATIVE_PATH / "run-summary.json"

SourceAttempt = dict[str, str]
SourceAttempts = tuple[SourceAttempt, ...]
_ATTEMPT_SOURCE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
_ATTEMPT_REASON_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_ATTEMPT_STATUSES = {"success", "failed", "unsupported"}
_ATTEMPT_REASON_CODES = {
    "ok",
    "provider_error",
    "invalid_result",
    "unsupported_asset",
    "missing_dependency",
    "empty_result",
    "missing_field",
    "invalid_response",
    "request_error",
    "query_error",
    "timeout",
}
_PUBLIC_DATA_SOURCES = {
    "eastmoney",
    "tushare",
    "adata",
    "tencent",
    "baostock",
    "failover",
    "public_daily",
}

_CODE_PATTERN = re.compile(r"^[0-9]{6}$")
_STATE_LABELS: dict[SwingState, str] = {
    SwingState.DATA_INSUFFICIENT: "数据不足",
    SwingState.LOW_WATCH: "低位观察",
    SwingState.BOTTOM_CONFIRMED: "低位反转信号",
    SwingState.NEUTRAL: "中性",
    SwingState.HIGH_WATCH: "高位观察",
    SwingState.TOP_CONFIRMED: "高位转弱信号",
}
_REASON_LABELS = {
    "history_or_indicator_insufficient": "历史日线或指标所需数据还不够",
    "low_price_position": "价格处在近一段时间相对偏低区域",
    "drawdown": "价格较滚动高点有明显回撤",
    "reversal_confirmation": "先进入高低位观察区，随后日线出现方向性跟随",
    "reversal_not_confirmed": "已经接近低位，但反转条件尚未同时满足",
    "high_price_position": "价格处在近一段时间相对偏高区域",
    "near_rolling_high": "价格接近滚动观察区间高点",
    "no_extreme_or_confirmation": "暂未同时出现明显高低位和反转确认",
    "ambiguous_extreme_zone": "高低位条件相互冲突，因此暂不作方向判断",
}
_PATTERN_LABELS = {
    "doji": "十字形态",
    "hammer": "锤子线",
    "bullish_engulfing": "看涨吞没形态",
    "shooting_star": "射击之星形态",
    "bearish_engulfing": "看跌吞没形态",
}
_UNUSED_INFORMATION = (
    "未使用财务报表、估值和盈利预测",
    "未使用新闻、公告、资金流和行业基本面",
    "未使用盘中分时、盘口和实时消息",
    "未使用个人成本价来改变状态判断",
)


class SwingServiceError(RuntimeError):
    """面向调用方的服务错误；错误消息不包含私有路径或持仓原始内容。"""


class SwingDataInsufficientError(SwingServiceError):
    """公开日线不足以安全计算本次结果。"""


class DailyHistoryProvider(Protocol):
    """服务所需的最小日线 provider 协议，便于测试注入模拟 provider。"""

    name: str

    def fetch_daily_bars(
        self,
        code: str,
        *,
        asset_type: str,
        as_of: date | datetime | None = None,
    ) -> Any:
        ...


@dataclass(frozen=True, slots=True)
class AnalysisResult:
    """单一持仓的中文观察结果，不包含任何执行指令。"""

    code: str
    name: str
    asset_type: str
    status: str
    state: SwingState
    state_label: str
    confidence: str
    strategy_version: str
    as_of: date | datetime
    data_as_of: date | None
    bars_available: int
    required_bars: int
    data_source: str
    adjustment: str
    reasons: tuple[str, ...] = ()
    unused_information: tuple[str, ...] = _UNUSED_INFORMATION
    features: Mapping[str, object] = None  # type: ignore[assignment]
    error: str | None = None
    report_path: str | None = None
    source_attempts: SourceAttempts = ()

    def __post_init__(self) -> None:
        if self.features is None:
            object.__setattr__(self, "features", {})

    @property
    def is_data_sufficient(self) -> bool:
        return self.status == "ok" and self.state is not SwingState.DATA_INSUFFICIENT

    def as_dict(self, *, include_name: bool = True) -> dict[str, object]:
        payload: dict[str, object] = {
            "code": self.code,
            "asset_type": self.asset_type,
            "mode": "analyze",
            "status": self.status,
            "state": self.state.value,
            "state_label": self.state_label,
            "confidence": self.confidence,
            "strategy_version": self.strategy_version,
            "as_of": _date_or_datetime_text(self.as_of),
            "data_as_of": self.data_as_of.isoformat() if self.data_as_of else None,
            "bars_available": self.bars_available,
            "required_bars": self.required_bars,
            "data_source": self.data_source,
            "adjustment": self.adjustment,
            "reasons": list(self.reasons),
            "unused_information": list(self.unused_information),
            "features": dict(self.features),
            "report_path": self.report_path,
            "source_attempts": _source_attempts_payload(self.source_attempts),
        }
        if include_name:
            payload["name"] = self.name
        if self.error:
            payload["error"] = self.error
        return payload


@dataclass(frozen=True, slots=True)
class BacktestReport:
    """单一持仓的历史模拟结果；数据不足时 performance 均保持为 ``None``。"""

    code: str
    name: str
    asset_type: str
    status: str
    state: SwingState
    state_label: str
    strategy_version: str
    data_as_of: date | None
    start_date: date | None
    end_date: date | None
    bars_available: int
    required_bars: int
    data_source: str
    adjustment: str
    tactical_weight: float
    core_weight: float
    costs: TransactionCosts
    buy_and_hold: PerformanceMetrics | None = None
    static_core_cash: PerformanceMetrics | None = None
    core_tactical: PerformanceMetrics | None = None
    trade_events: int = 0
    deferred_count: int = 0
    robustness: Mapping[str, object] | None = None
    error: str | None = None
    report_path: str | None = None
    source_attempts: SourceAttempts = ()

    @property
    def has_performance(self) -> bool:
        return (
            self.status == "ok"
            and self.buy_and_hold is not None
            and self.static_core_cash is not None
            and self.core_tactical is not None
        )

    def as_dict(self, *, include_name: bool = True) -> dict[str, object]:
        payload: dict[str, object] = {
            "code": self.code,
            "asset_type": self.asset_type,
            "mode": "backtest",
            "status": self.status,
            "state": self.state.value,
            "state_label": self.state_label,
            "strategy_version": self.strategy_version,
            "data_as_of": self.data_as_of.isoformat() if self.data_as_of else None,
            "start_date": self.start_date.isoformat() if self.start_date else None,
            "end_date": self.end_date.isoformat() if self.end_date else None,
            "bars_available": self.bars_available,
            "required_bars": self.required_bars,
            "data_source": self.data_source,
            "adjustment": self.adjustment,
            "assumptions": {
                "tactical_weight": self.tactical_weight,
                "core_weight": self.core_weight,
                "frequency": "daily_close_to_next_open",
                "horizon": "weeks_to_months",
                "core_is_unchanged": True,
                "not_actual_return": True,
                "warmup_excluded": True,
                "drawdown_basis": "daily_close",
                "zero_liquidity_action": "defer",
                "adjusted_price_scope": "relative_rule_replay",
                "not_portfolio_aggregate": True,
                "costs_bps": {
                    "commission": self.costs.commission_bps,
                    "slippage": self.costs.slippage_bps,
                    "sell_tax": self.costs.sell_tax_bps,
                },
            },
            "buy_and_hold": _metrics_dict(self.buy_and_hold),
            "static_core_cash": _metrics_dict(self.static_core_cash),
            "core_tactical": _metrics_dict(self.core_tactical),
            "trade_events": self.trade_events,
            "deferred_count": self.deferred_count,
            "robustness": dict(self.robustness) if self.robustness is not None else None,
            "report_path": self.report_path,
            "source_attempts": _source_attempts_payload(self.source_attempts),
        }
        if include_name:
            payload["name"] = self.name
        if self.error:
            payload["error"] = self.error
        return payload


@dataclass(frozen=True, slots=True)
class SwingRun:
    """一次 CLI/Web 运行的内存结果。"""

    mode: str
    generated_at: datetime
    results: tuple[AnalysisResult | BacktestReport, ...]
    latest_results_path: Path
    run_summary_path: Path

    @property
    def report_paths(self) -> tuple[str, ...]:
        return tuple(
            item.report_path for item in self.results if item.report_path is not None
        )

    @property
    def report_path(self) -> str | None:
        paths = self.report_paths
        return paths[0] if len(paths) == 1 else None

    def as_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "generated_at": self.generated_at.isoformat(timespec="seconds"),
            "results": [item.as_dict() for item in self.results],
            "report_paths": list(self.report_paths),
        }


@dataclass(frozen=True, slots=True)
class _FetchedHistory:
    bars: tuple[DailyBar, ...]
    data_source: str
    adjustment: str
    completed_through: date | None
    source_attempts: SourceAttempts = ()


def _date_or_datetime_text(value: date | datetime) -> str:
    return value.isoformat(timespec="seconds") if isinstance(value, datetime) else value.isoformat()


def _metrics_dict(metrics: PerformanceMetrics | None) -> dict[str, object] | None:
    if metrics is None:
        return None
    return {
        "total_return": metrics.total_return,
        "max_drawdown": metrics.max_drawdown,
        "trade_count": metrics.trade_count,
        "turnover": metrics.turnover,
        "final_value": metrics.final_value,
        "annualized_return": metrics.annualized_return,
        "annualized_volatility": metrics.annualized_volatility,
        "calmar_ratio": metrics.calmar_ratio,
        "market_exposure": metrics.market_exposure,
    }


def _as_shanghai(value: date | datetime | None, now: Callable[[], datetime]) -> date | datetime:
    if value is not None:
        if isinstance(value, datetime) and value.tzinfo is not None:
            return value.astimezone(SHANGHAI)
        return value
    current = now()
    if current.tzinfo is None:
        return current.replace(tzinfo=SHANGHAI)
    return current.astimezone(SHANGHAI)


def _resolve_repo_root(value: str | Path | None) -> Path:
    root = (Path(value) if value is not None else DEFAULT_REPO_ROOT).resolve()
    if not root.exists() or not root.is_dir():
        raise SwingServiceError("仓库目录不可用")
    return root


def resolve_private_root(repo_root: str | Path | None = None) -> Path:
    """解析固定的 ``<repo>/data/private``，拒绝 symlink/路径越界。"""

    root = _resolve_repo_root(repo_root)
    data_root = (root / "data").resolve()
    private_root = (data_root / "private").resolve()
    try:
        if data_root.parent != root or private_root.parent != data_root or private_root.name != "private":
            raise ValueError
    except ValueError as exc:
        raise SwingServiceError("私有数据目录必须位于仓库 data/private 下") from exc
    return private_root


def _ensure_private_child(repo_root: Path, path: Path) -> Path:
    """将内部路径解析并确认仍位于固定私有目录。"""

    private_root = resolve_private_root(repo_root)
    candidate = path.resolve()
    try:
        candidate.relative_to(private_root)
    except ValueError as exc:
        raise SwingServiceError("报告路径必须位于本机私有目录") from exc
    return candidate


def _safe_code(code: str) -> str:
    normalized = str(code).strip()
    if _CODE_PATTERN.fullmatch(normalized) is None:
        raise SwingServiceError("证券代码格式不受支持")
    return normalized


def _history_bar_to_daily(bar: Any) -> DailyBar:
    trade_date = getattr(bar, "trade_date", None)
    if trade_date is None:
        trade_date = getattr(bar, "date", None)
    if isinstance(trade_date, datetime):
        moment = trade_date
    elif isinstance(trade_date, date):
        moment = datetime.combine(trade_date, time(15, 0), tzinfo=SHANGHAI)
    else:
        raise TypeError("公开日线缺少交易日期")

    def field(*names: str) -> float:
        for name in names:
            value = getattr(bar, name, None)
            if value is not None:
                return float(value)
        raise ValueError("公开日线字段不完整")

    return DailyBar(
        trade_date=moment,
        open_price=field("open_price", "open"),
        high_price=field("high_price", "high"),
        low_price=field("low_price", "low"),
        close_price=field("close_price", "close"),
        volume=field("volume"),
        turnover_amount=field("turnover_amount", "amount"),
    )


def _normalise_source_attempts(value: object) -> SourceAttempts:
    """Keep only the small public attempt contract from a provider result."""

    if value is None:
        return ()
    if isinstance(value, Mapping):
        candidates: Sequence[object] = (value,)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        candidates = value
    else:
        return ()

    safe: list[SourceAttempt] = []
    for item in candidates:
        candidate: object = item
        as_dict = getattr(item, "as_dict", None)
        if callable(as_dict):
            try:
                candidate = as_dict()
            except Exception:  # noqa: BLE001 - metadata must not break the result
                candidate = None
        if candidate is None:
            continue
        if isinstance(candidate, Mapping):
            source = candidate.get("source")
            status = candidate.get("status")
            reason_code = candidate.get("reason_code", candidate.get("public_reason_code"))
        else:
            source = getattr(candidate, "source", None)
            status = getattr(candidate, "status", None)
            reason_code = getattr(candidate, "reason_code", getattr(candidate, "public_reason_code", None))
        if not all(isinstance(field, str) for field in (source, status, reason_code)):
            continue
        normalized_source = source.strip().lower()
        normalized_status = status.strip().lower()
        normalized_reason = reason_code.strip().lower()
        if (
            _ATTEMPT_SOURCE_PATTERN.fullmatch(normalized_source)
            and normalized_status in _ATTEMPT_STATUSES
            and _ATTEMPT_REASON_PATTERN.fullmatch(normalized_reason)
            and normalized_reason in _ATTEMPT_REASON_CODES
            and not any(
                marker in normalized_reason
                for marker in ("token", "http", "url", "exception", "trace")
            )
        ):
            safe.append(
                {
                    "source": normalized_source,
                    "status": normalized_status,
                    "reason_code": normalized_reason,
                }
            )
    return tuple(safe)


def _source_attempts_payload(value: object) -> list[dict[str, str]]:
    return [dict(attempt) for attempt in _normalise_source_attempts(value)]


def _safe_data_source(value: object) -> str:
    source = str(value).strip().lower()
    return source if source in _PUBLIC_DATA_SOURCES else "public_daily"


def _attempts_from_failure(exc: BaseException) -> SourceAttempts:
    if isinstance(exc, HistoryFailoverError):
        return _normalise_source_attempts(getattr(exc, "attempts", ()))
    return ()


def _build_default_history_provider():
    """Build default public history routes, adding Tushare only with a token."""

    token = os.environ.get("TUSHARE_TOKEN", "")
    additional: list[object] = []
    if isinstance(token, str) and token.strip():
        # Import the optional provider only after an explicit non-empty token is
        # present. The provider itself also lazy-loads the optional tushare SDK.
        from trading_agent.swing.tushare_history import TushareHistoryProvider

        additional.append(TushareHistoryProvider(token=token.strip()))
    return build_default_history_provider(additional)


def _normalise_history(raw: Any, provider: DailyHistoryProvider) -> _FetchedHistory:
    metadata = raw if isinstance(raw, HistoryData) else None
    source = _safe_data_source(
        getattr(raw, "source", getattr(provider, "name", "public_daily"))
    )
    adjustment = str(getattr(raw, "adjustment", "qfq"))
    source_attempts = _normalise_source_attempts(
        getattr(raw, "source_attempts", getattr(provider, "source_attempts", ()))
    )
    raw_bars: Any = getattr(raw, "bars", raw)
    if isinstance(raw_bars, (str, bytes)) or not isinstance(raw_bars, Iterable):
        raise TypeError("公开日线返回格式不受支持")
    converted = tuple(_history_bar_to_daily(item) for item in raw_bars)
    completed = getattr(metadata, "completed_through", None)
    if not isinstance(completed, date):
        completed = converted[-1].trade_date.date() if converted else None
    return _FetchedHistory(
        bars=converted,
        data_source=source,
        adjustment=adjustment,
        completed_through=completed,
        source_attempts=source_attempts,
    )


def _translate_reasons(decision: SwingDecision) -> tuple[str, ...]:
    values: list[str] = []
    for reason in decision.reasons:
        translated = _REASON_LABELS.get(reason, reason)
        if translated not in values:
            values.append(translated)
    patterns = [_PATTERN_LABELS[name] for name in decision.features.candle_patterns if name in _PATTERN_LABELS]
    if patterns:
        values.append("当日辅助形态：" + "、".join(patterns))
    return tuple(values)


def _feature_dict(decision: SwingDecision | None) -> dict[str, object]:
    if decision is None:
        return {}
    features = decision.features

    def rounded(value: float | None) -> float | None:
        return round(value, 4) if value is not None and math.isfinite(value) else None

    return {
        "close_price": rounded(features.close_price),
        "price_position": rounded(features.price_position),
        "drawdown": rounded(features.drawdown),
        "ma_fast": rounded(features.ma_fast),
        "ma_slow": rounded(features.ma_slow),
        "rsi": rounded(features.rsi),
        "atr_pct": rounded(features.atr_pct),
        "bollinger_percent_b": rounded(features.bollinger_percent_b),
        "relative_volume": rounded(features.relative_volume),
        "trend_regime": features.trend_regime,
        "candle_patterns": [_PATTERN_LABELS.get(name, name) for name in features.candle_patterns],
    }


def _generic_data_error() -> str:
    # 不回显 provider 原始异常，避免把绝对路径、请求参数或个人内容写进错误。
    return "公开日线暂时不可用或未通过完整性校验，未生成方向状态或历史收益结论。"


class SwingService:
    """本机持仓波段观察的同步编排服务。"""

    def __init__(
        self,
        repo_root: str | Path | None = None,
        *,
        portfolio_store: PortfolioStore | None = None,
        provider: DailyHistoryProvider | None = None,
        config: SwingConfig | None = None,
        costs: TransactionCosts | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.repo_root = _resolve_repo_root(repo_root)
        self.private_root = resolve_private_root(self.repo_root)
        self.private_root.mkdir(parents=True, exist_ok=True)
        if portfolio_store is None:
            portfolio_store = PortfolioStore(self.private_root / "holdings.json")
        else:
            store_path = Path(portfolio_store.path).resolve()
            if store_path.parent != self.private_root or store_path.name != "holdings.json":
                raise SwingServiceError("持仓文件必须固定在 data/private/holdings.json")
        self.portfolio_store = portfolio_store
        self.provider = provider if provider is not None else _build_default_history_provider()
        self.config = config or SwingConfig()
        self.costs = costs
        self._now = now or (lambda: datetime.now(SHANGHAI))

    def _costs_for(self, holding: Holding) -> TransactionCosts:
        """返回本次历史模拟的公开成本假设，不读取券商账户。"""

        if self.costs is not None:
            return self.costs
        return TransactionCosts(
            commission_bps=DEFAULT_COMMISSION_BPS,
            slippage_bps=DEFAULT_SLIPPAGE_BPS,
            sell_tax_bps=(
                DEFAULT_STOCK_SELL_TAX_BPS
                if holding.asset_type == "stock"
                else DEFAULT_ETF_SELL_TAX_BPS
            ),
        )

    @property
    def reports_root(self) -> Path:
        return _ensure_private_child(self.repo_root, self.private_root / "reports")

    @property
    def latest_results_path(self) -> Path:
        return _ensure_private_child(self.repo_root, self.private_root / "latest-results.json")

    @property
    def run_summary_path(self) -> Path:
        return _ensure_private_child(self.repo_root, self.private_root / "run-summary.json")

    def load_latest_results(self) -> dict[str, object]:
        """读取 Web 可消费的最近结果；不存在时返回稳定空结构。"""

        path = self.latest_results_path
        if not path.exists():
            return {"schema_version": 1, "updated_at": None, "results": []}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise SwingServiceError("最近结果文件暂时无法读取") from exc
        if not isinstance(raw, dict) or raw.get("schema_version") != 1 or not isinstance(raw.get("results"), list):
            raise SwingServiceError("最近结果文件格式无效")
        return raw

    def analyze(
        self,
        code: str | None = None,
        *,
        asset_type: str | None = None,
        as_of: date | datetime | None = None,
    ) -> AnalysisResult | SwingRun:
        """分析单个持仓；不传代码时分析全部本机持仓并写私有报告。"""

        if code is None:
            if asset_type is not None:
                raise SwingServiceError("未指定代码时不能指定资产类型")
            return self.run("analyze", as_of=as_of)
        run = self.run("analyze", code=code, asset_type=asset_type, as_of=as_of)
        if not run.results:
            raise SwingServiceError("没有可分析的本机持仓")
        result = run.results[0]
        if not isinstance(result, AnalysisResult):
            raise SwingServiceError("分析结果类型异常")
        return result

    def analyze_all(self, *, as_of: date | datetime | None = None) -> SwingRun:
        return self.run("analyze", as_of=as_of)

    def backtest(
        self,
        code: str | None = None,
        *,
        asset_type: str | None = None,
        as_of: date | datetime | None = None,
    ) -> BacktestReport | SwingRun:
        """对单个持仓做历史模拟；不传代码时处理全部本机持仓。"""

        if code is None:
            if asset_type is not None:
                raise SwingServiceError("未指定代码时不能指定资产类型")
            return self.run("backtest", as_of=as_of)
        run = self.run("backtest", code=code, asset_type=asset_type, as_of=as_of)
        if not run.results:
            raise SwingServiceError("没有可回放的本机持仓")
        result = run.results[0]
        if not isinstance(result, BacktestReport):
            raise SwingServiceError("回放结果类型异常")
        return result

    def backtest_all(self, *, as_of: date | datetime | None = None) -> SwingRun:
        return self.run("backtest", as_of=as_of)

    def run(
        self,
        mode: str,
        *,
        code: str | None = None,
        asset_type: str | None = None,
        as_of: date | datetime | None = None,
    ) -> SwingRun:
        """运行 ``analyze``、``backtest`` 或 ``all`` 并原子更新私有摘要。"""

        if mode not in {"analyze", "backtest", "all"}:
            raise SwingServiceError("运行模式必须是 analyze、backtest 或 all")
        selected = self._select_holdings(code, asset_type=asset_type)
        observed_at = _as_shanghai(as_of, self._now)
        generated_at = self._as_datetime(observed_at)
        modes = ("analyze", "backtest") if mode == "all" else (mode,)
        result_items: list[AnalysisResult | BacktestReport] = []

        for holding in selected:
            for item_mode in modes:
                if item_mode == "analyze":
                    item = self._analyze_holding(holding, observed_at)
                    report_text = self._render_analysis(item)
                    report_kind = "analysis"
                else:
                    item = self._backtest_holding(holding, observed_at)
                    report_text = self._render_backtest(item)
                    report_kind = "backtest"
                relative_path = self._write_report(
                    report_kind,
                    holding.code,
                    report_text,
                    generated_at,
                )
                item = replace(item, report_path=relative_path)
                result_items.append(item)

        latest = self._update_latest_results(result_items, generated_at)
        self._write_run_summary(mode, result_items, generated_at)
        return SwingRun(
            mode=mode,
            generated_at=generated_at,
            results=tuple(result_items),
            latest_results_path=latest,
            run_summary_path=self.run_summary_path,
        )

    def _as_datetime(self, value: date | datetime) -> datetime:
        if isinstance(value, datetime):
            return value if value.tzinfo is not None else value.replace(tzinfo=SHANGHAI)
        return datetime.combine(value, time(15, 0), tzinfo=SHANGHAI)

    def _select_holdings(
        self, code: str | None, *, asset_type: str | None = None
    ) -> tuple[Holding, ...]:
        snapshot: PortfolioSnapshot = self.portfolio_store.load()
        if code is None:
            if asset_type is not None:
                raise SwingServiceError("未指定代码时不能指定资产类型")
            return snapshot.holdings
        normalized = _safe_code(code)
        if asset_type is not None and asset_type not in {"stock", "etf"}:
            raise SwingServiceError("资产类型格式不受支持")
        matches = tuple(
            item
            for item in snapshot.holdings
            if item.code == normalized and (asset_type is None or item.asset_type == asset_type)
        )
        if not matches:
            raise SwingServiceError("未找到指定的本机持仓")
        if len(matches) > 1:
            raise SwingServiceError("同一代码对应多个持仓，无法安全选择")
        return matches

    def _fetch(self, holding: Holding, observed_at: date | datetime) -> _FetchedHistory:
        raw = self.provider.fetch_daily_bars(
            holding.code,
            asset_type=holding.asset_type,
            as_of=observed_at,
        )
        history = _normalise_history(raw, self.provider)
        prepared = prepare_bars(history.bars, as_of=observed_at)
        return replace(history, bars=prepared)

    def _analyze_holding(self, holding: Holding, observed_at: date | datetime) -> AnalysisResult:
        try:
            history = self._fetch(holding, observed_at)
            if not history.bars:
                raise SwingDataInsufficientError("empty")
            features = calculate_swing_features(history.bars, config=self.config)
            decisions = SwingStateMachine(self.config).evaluate(features)
            decision = decisions[-1] if decisions else None
            if decision is None:
                raise SwingDataInsufficientError("empty")
            data_as_of = history.bars[-1].trade_date.date()
            bars_available = len(history.bars)
            if bars_available < self.config.min_history_bars:
                state = SwingState.DATA_INSUFFICIENT
                state_label = _STATE_LABELS[state]
                status = "data_insufficient"
                confidence = "较低"
                reasons = ("历史日线数量少于最低要求，暂不生成方向状态",)
            else:
                state = decision.state
                state_label = _STATE_LABELS[state]
                status = "ok"
                confidence = {"low": "较低", "medium": "中等", "high": "较高"}.get(
                    decision.confidence, decision.confidence
                )
                reasons = _translate_reasons(decision)
            return AnalysisResult(
                code=holding.code,
                name=holding.name,
                asset_type=holding.asset_type,
                status=status,
                state=state,
                state_label=state_label,
                confidence=confidence,
                strategy_version=STRATEGY_VERSION,
                as_of=observed_at,
                data_as_of=data_as_of,
                bars_available=bars_available,
                required_bars=self.config.min_history_bars,
                data_source=history.data_source,
                adjustment=history.adjustment,
                reasons=reasons,
                features=_feature_dict(decision),
                source_attempts=history.source_attempts,
            )
        except Exception as exc:  # noqa: BLE001 - safe public result boundary
            failure_attempts = _attempts_from_failure(exc)
            if isinstance(exc, SwingDataInsufficientError):
                return AnalysisResult(
                    code=holding.code,
                    name=holding.name,
                    asset_type=holding.asset_type,
                    status="data_insufficient",
                    state=SwingState.DATA_INSUFFICIENT,
                    state_label=_STATE_LABELS[SwingState.DATA_INSUFFICIENT],
                    confidence="较低",
                    strategy_version=STRATEGY_VERSION,
                    as_of=observed_at,
                    data_as_of=None,
                    bars_available=0,
                    required_bars=self.config.min_history_bars,
                    data_source=_safe_data_source(getattr(self.provider, "name", "public_daily")),
                    adjustment="qfq",
                    reasons=("没有可用的完整收盘日线",),
                    error=_generic_data_error(),
                    source_attempts=failure_attempts,
                )
            return AnalysisResult(
                code=holding.code,
                name=holding.name,
                asset_type=holding.asset_type,
                status="error",
                state=SwingState.DATA_INSUFFICIENT,
                state_label=_STATE_LABELS[SwingState.DATA_INSUFFICIENT],
                confidence="较低",
                strategy_version=STRATEGY_VERSION,
                as_of=observed_at,
                data_as_of=None,
                bars_available=0,
                required_bars=self.config.min_history_bars,
                data_source=_safe_data_source(getattr(self.provider, "name", "public_daily")),
                adjustment="qfq",
                reasons=("公开日线未通过安全校验",),
                error=_generic_data_error(),
                source_attempts=failure_attempts,
            )

    def _backtest_holding(self, holding: Holding, observed_at: date | datetime) -> BacktestReport:
        effective_costs = self._costs_for(holding)
        try:
            holding_config = replace(
                self.config,
                tactical_weight=holding.tactical_ratio,
                core_weight=1.0 - holding.tactical_ratio,
            )
            history = self._fetch(holding, observed_at)
            bars = history.bars
            state = SwingState.DATA_INSUFFICIENT
            state_label = _STATE_LABELS[state]
            if bars:
                features = calculate_swing_features(bars, config=holding_config)
                if features:
                    latest = SwingStateMachine(holding_config).evaluate(features)[-1]
                    state = latest.state
                    state_label = _STATE_LABELS[state]
            common: dict[str, object] = {
                "code": holding.code,
                "name": holding.name,
                "asset_type": holding.asset_type,
                "strategy_version": STRATEGY_VERSION,
                "data_as_of": bars[-1].trade_date.date() if bars else None,
                "bars_available": len(bars),
                "required_bars": holding_config.min_history_bars,
                "data_source": history.data_source,
                "adjustment": history.adjustment,
                "tactical_weight": holding_config.tactical_weight,
                "core_weight": holding_config.core_weight,
                "costs": effective_costs,
                "state": state,
                "state_label": state_label,
                "source_attempts": history.source_attempts,
            }
            if len(bars) < holding_config.min_history_bars:
                return BacktestReport(
                    **common,
                    status="data_insufficient",
                    start_date=bars[0].trade_date.date() if bars else None,
                    end_date=bars[-1].trade_date.date() if bars else None,
                    error="历史日线少于最低要求，未计算任何收益结论。",
                )
            result: BacktestResult = run_backtest(
                bars,
                asset_type=holding.asset_type,
                costs=effective_costs,
                config=holding_config,
            )
            robustness = run_robustness_checks(
                bars,
                asset_type=holding.asset_type,
                costs=effective_costs,
                config=holding_config,
                base_result=result,
            )
            return BacktestReport(
                **common,
                status="ok",
                start_date=result.start_date.date(),
                end_date=result.end_date.date(),
                buy_and_hold=result.buy_and_hold,
                static_core_cash=result.static_core_cash,
                core_tactical=result.core_tactical,
                trade_events=len(result.trades),
                deferred_count=result.deferred_count,
                robustness=robustness.as_dict(),
            )
        except Exception as exc:  # noqa: BLE001 - safe public result boundary
            return BacktestReport(
                code=holding.code,
                name=holding.name,
                asset_type=holding.asset_type,
                status="error",
                state=SwingState.DATA_INSUFFICIENT,
                state_label=_STATE_LABELS[SwingState.DATA_INSUFFICIENT],
                strategy_version=STRATEGY_VERSION,
                data_as_of=None,
                start_date=None,
                end_date=None,
                bars_available=0,
                required_bars=self.config.min_history_bars,
                data_source=_safe_data_source(getattr(self.provider, "name", "public_daily")),
                adjustment="qfq",
                tactical_weight=holding.tactical_ratio,
                core_weight=1.0 - holding.tactical_ratio,
                costs=effective_costs,
                error=_generic_data_error(),
                source_attempts=_attempts_from_failure(exc),
            )

    def _render_analysis(self, result: AnalysisResult) -> str:
        from trading_agent.swing.reports import render_analysis_report

        return render_analysis_report(result)

    def _render_backtest(self, result: BacktestReport) -> str:
        from trading_agent.swing.reports import render_backtest_report

        return render_backtest_report(result)

    def _write_report(
        self,
        kind: str,
        code: str,
        content: str,
        generated_at: datetime,
    ) -> str:
        from trading_agent.swing.reports import write_private_report

        absolute = write_private_report(
            self.repo_root,
            kind=kind,
            code=_safe_code(code),
            content=content,
            generated_at=generated_at,
        )
        return _relative_repo_path(self.repo_root, absolute)

    def _update_latest_results(
        self,
        results: Sequence[AnalysisResult | BacktestReport],
        generated_at: datetime,
    ) -> Path:
        from trading_agent.swing.reports import atomic_write_json

        current = self.load_latest_results()
        old_items = current.get("results", [])
        merged: dict[tuple[str, str, str], dict[str, object]] = {}
        if isinstance(old_items, list):
            for item in old_items:
                if isinstance(item, Mapping):
                    code = item.get("code")
                    asset_type = item.get("asset_type")
                    mode = item.get("mode")
                    if isinstance(code, str) and isinstance(asset_type, str) and isinstance(mode, str):
                        # 只继承 Web 合同字段，并重新验证旧 report_path；即使
                        # 用户手工改过私有 JSON，也不能让绝对路径或 ``..``
                        # 路径进入下一份可消费摘要。
                        entry: dict[str, object] = {
                            "code": code,
                            "asset_type": asset_type,
                            "mode": mode,
                            "status": item.get("status", "unknown"),
                            "state": item.get("state", "DATA_INSUFFICIENT"),
                            "data_as_of": item.get("data_as_of"),
                            "report_path": self._safe_report_reference(item.get("report_path")),
                            "data_source": _safe_data_source(item.get("data_source")),
                            "source_attempts": _source_attempts_payload(
                                item.get("source_attempts", ())
                            ),
                        }
                        if mode == "backtest":
                            for summary_key in (
                                "assumptions",
                                "buy_and_hold",
                                "static_core_cash",
                                "core_tactical",
                                "trade_events",
                                "deferred_count",
                                "robustness",
                            ):
                                if summary_key in item:
                                    entry[summary_key] = item.get(summary_key)
                        if isinstance(item.get("error"), str):
                            entry["error"] = "最近运行存在数据边界，请重新运行查看。"
                        merged[(code, asset_type, mode)] = entry
        for item in results:
            payload = item.as_dict(include_name=False)
            # Web contract intentionally contains only a repo-relative path.
            payload = {
                key: payload[key]
                for key in (
                    "code",
                    "asset_type",
                    "mode",
                    "status",
                    "state",
                    "data_source",
                    "data_as_of",
                    "report_path",
                    "error",
                    "assumptions",
                    "buy_and_hold",
                    "static_core_cash",
                    "core_tactical",
                    "trade_events",
                    "deferred_count",
                    "robustness",
                    "source_attempts",
                )
                if key in payload
            }
            payload["source_attempts"] = _source_attempts_payload(
                payload.get("source_attempts", ())
            )
            payload["data_source"] = _safe_data_source(payload.get("data_source"))
            merged[(str(payload["code"]), str(payload["asset_type"]), str(payload["mode"]))] = payload
        document = {
            "schema_version": 1,
            "updated_at": generated_at.isoformat(timespec="seconds"),
            "results": list(merged.values()),
        }
        path = self.latest_results_path
        atomic_write_json(path, document, repo_root=self.repo_root)
        return path

    def _safe_report_reference(self, value: object) -> str | None:
        if not isinstance(value, str) or not value or Path(value).is_absolute():
            return None
        try:
            candidate = _ensure_private_child(self.repo_root, self.repo_root / value)
            candidate.relative_to(self.reports_root)
        except (SwingServiceError, ValueError):
            return None
        if candidate.suffix.casefold() != ".md":
            return None
        return _relative_repo_path(self.repo_root, candidate)

    def _write_run_summary(
        self,
        mode: str,
        results: Sequence[AnalysisResult | BacktestReport],
        generated_at: datetime,
    ) -> None:
        from trading_agent.swing.reports import atomic_write_json

        document = {
            "schema_version": 1,
            "mode": mode,
            "updated_at": generated_at.isoformat(timespec="seconds"),
            "result_count": len(results),
            "results": [
                {
                    "code": item.code,
                    "asset_type": item.asset_type,
                    "mode": "analyze" if isinstance(item, AnalysisResult) else "backtest",
                    "status": item.status,
                    "state": item.state.value,
                    "data_as_of": (
                        item.data_as_of.isoformat() if item.data_as_of else None
                    ),
                    "report_path": item.report_path,
                }
                for item in results
            ],
        }
        atomic_write_json(self.run_summary_path, document, repo_root=self.repo_root)


def _relative_repo_path(repo_root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError as exc:
        raise SwingServiceError("报告路径不在仓库内") from exc


def load_latest_results(repo_root: str | Path | None = None) -> dict[str, object]:
    """无副作用读取固定的最近结果文件，供 Web 层解耦调用。"""

    service = SwingService(repo_root)
    return service.load_latest_results()


def latest_results_path(repo_root: str | Path | None = None) -> Path:
    """返回固定最近结果文件路径，不接受请求方提供的相对路径。"""

    return SwingService(repo_root).latest_results_path


__all__ = [
    "DEFAULT_REPO_ROOT",
    "LATEST_RESULTS_RELATIVE_PATH",
    "PRIVATE_RELATIVE_PATH",
    "REPORTS_RELATIVE_PATH",
    "RUN_SUMMARY_RELATIVE_PATH",
    "STRATEGY_VERSION",
    "AnalysisResult",
    "BacktestReport",
    "SwingDataInsufficientError",
    "SwingRun",
    "SwingService",
    "SwingServiceError",
    "latest_results_path",
    "load_latest_results",
    "resolve_private_root",
]
