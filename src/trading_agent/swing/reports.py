"""本机私有的中文持仓波段报告和运行摘要写入器。

报告面向量化初学者，使用“观察状态”语言，不生成执行指令。所有写入路径
都由 ``repo_root/data/private`` 推导并在解析后再次校验；调用方不能通过
报告名称或路径把内容写到仓库外。
"""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from trading_agent.swing.service import AnalysisResult, BacktestReport


_CODE_PATTERN = re.compile(r"^[0-9]{6}$")
_PRIVATE_NAME = "private"


class SwingReportError(RuntimeError):
    """私有报告无法安全写入。"""


def _resolve_private_root(repo_root: str | Path) -> tuple[Path, Path]:
    root = Path(repo_root).resolve()
    if not root.exists() or not root.is_dir():
        raise SwingReportError("仓库目录不可用")
    data_root = (root / "data").resolve()
    private_root = (data_root / _PRIVATE_NAME).resolve()
    if data_root.parent != root or private_root.parent != data_root or private_root.name != _PRIVATE_NAME:
        raise SwingReportError("私有数据目录必须位于仓库 data/private 下")
    return root, private_root


def _private_child(repo_root: str | Path, path: str | Path) -> tuple[Path, Path]:
    root, private_root = _resolve_private_root(repo_root)
    candidate = Path(path).resolve()
    try:
        candidate.relative_to(private_root)
    except ValueError as exc:
        raise SwingReportError("报告路径必须位于本机私有目录") from exc
    return root, candidate


def _date_text(value: object) -> str:
    return str(value) if value is not None else "未取得"


def _percent(value: float | None) -> str:
    if value is None or not math.isfinite(value):
        return "未计算"
    return f"{value:.2%}"


def _number(value: object, digits: int = 4) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "未取得"
    value_float = float(value)
    return f"{value_float:.{digits}f}" if math.isfinite(value_float) else "未取得"


def _state_heading(result: AnalysisResult | BacktestReport) -> str:
    return f"{result.state_label}（{result.state.value}）"


def render_analysis_report(result: AnalysisResult) -> str:
    """渲染单持仓观察报告；不输出个人数量、成本等非必要字段。"""

    feature = result.features
    reasons = list(result.reasons) or ["没有足够的结构化依据"]
    unused = list(result.unused_information)
    pattern = feature.get("candle_patterns") if isinstance(feature, Mapping) else None
    pattern_text = "、".join(str(item) for item in pattern) if isinstance(pattern, list) and pattern else "未识别到辅助形态"
    lines = [
        "# 持仓波段观察报告",
        "",
        "> 本报告服务于数周至数月的日线观察，不是盘中或高频系统。",
        "> 本报告只描述状态、依据和数据边界，不构成任何执行指令。",
        "",
        "## 先看结论",
        "",
        f"- 标的：{result.name}（代码仅用于本机索引）",
        f"- 观察状态：{_state_heading(result)}",
        f"- 置信度：{result.confidence}",
        f"- 数据截止：{_date_text(result.data_as_of)}",
        f"- 数据量：{result.bars_available} 根日线（最低要求 {result.required_bars} 根）",
        f"- 策略版本：{result.strategy_version}",
        f"- 数据源：{result.data_source}；复权方式：{result.adjustment}",
        "",
        "## 用小白能理解的话解释",
        "",
        "这里把价格放到一段较长的历史区间里观察：先看它是在区间偏低、偏高还是中间；",
        "再看趋势、动能和 K 线是否出现相互支持的回稳/转弱迹象。单个形态不能单独决定结论。",
        "",
        "## 本次依据",
        "",
    ]
    lines.extend(f"- {reason}" for reason in reasons)
    lines.extend(
        [
            f"- 趋势环境：{feature.get('trend_regime', '未取得')}",
            f"- 区间位置：{_number(feature.get('price_position'))}（0 接近区间低端，1 接近区间高端）",
            f"- 相对回撤：{_percent(feature.get('drawdown'))}",
            f"- RSI 动能：{_number(feature.get('rsi'), 2)}（只作辅助，不单独使用）",
            f"- 布林带位置：{_number(feature.get('bollinger_percent_b'), 2)}",
            f"- 成交量相对值：{_number(feature.get('relative_volume'), 2)}",
            f"- 辅助 K 线形态：{pattern_text}",
            "",
            "## 未使用的信息",
            "",
        ]
    )
    lines.extend(f"- {item}" for item in unused)
    if result.error:
        lines.extend(["", "## 数据边界", "", f"- {result.error}"])
    lines.extend(
        [
            "",
            "## 使用边界",
            "",
            "公开行情可能延迟、缺行或临时不可用；数据不足时本报告不会把缺失数据补成结论。",
            "技术指标只反映历史价格行为，不能代表未来结果；仍需结合持仓者自己的基本面判断和风险承受能力。",
            "",
        ]
    )
    return "\n".join(lines)


def _metrics_lines(title: str, metrics: Any) -> list[str]:
    if metrics is None:
        return [f"- {title}：未计算"]
    return [
        f"- {title}总收益（历史模拟）：{_percent(metrics.total_return)}",
        f"- {title}最大回撤：{_percent(metrics.max_drawdown)}",
        f"- {title}最终净值（初始 1.0）：{_number(metrics.final_value, 4)}",
        f"- {title}状态变化次数：{metrics.trade_count}",
        f"- {title}换手比例：{_percent(metrics.turnover)}",
    ]


def render_backtest_report(result: BacktestReport) -> str:
    """渲染历史回放报告；数据不足时不展示任何收益数字。"""

    lines = [
        "# 持仓波段历史模拟报告",
        "",
        "> 本报告是历史数据回放，不是用户真实收益，也不代表未来结果。",
        "> 本报告服务于数周至数月的日线观察，不是盘中或高频系统。",
        "> 本报告只描述模拟假设和历史统计，不构成任何执行指令。",
        "",
        "## 回放概况",
        "",
        f"- 标的：{result.name}（代码仅用于本机索引）",
        f"- 最新观察状态：{_state_heading(result)}",
        f"- 数据区间：{_date_text(result.start_date)} 至 {_date_text(result.end_date)}",
        f"- 数据截止：{_date_text(result.data_as_of)}",
        f"- 数据量：{result.bars_available} 根日线（最低要求 {result.required_bars} 根）",
        f"- 策略版本：{result.strategy_version}",
        f"- 数据源：{result.data_source}；复权方式：{result.adjustment}",
        "",
        "## 模拟假设",
        "",
        "- 比较基准：全程持有同一标的的历史净值曲线。",
        f"- 机动模拟比例：{result.tactical_weight:.0%}，这是可编辑的假设，不是个人实际配置；核心仓比例 {result.core_weight:.0%} 全程保持不变。",
        "- 时序规则：当天收盘形成状态，下一根日线开盘才处理模拟暴露变化。",
        "- 频率与期限：日线、数周至数月、中低频；最短持有期和冷静期用于减少来回变化。",
        f"- 成本假设：手续费 {result.costs.commission_bps:g} bps、滑点 {result.costs.slippage_bps:g} bps、单向税费 {result.costs.sell_tax_bps:g} bps；真实费率需自行核对。",
        "",
    ]
    if result.status != "ok" or not result.has_performance:
        lines.extend(
            [
                "## 结果",
                "",
                "- 数据不足或未通过完整性检查，本次不生成任何收益结论。",
            ]
        )
        if result.error:
            lines.append(f"- 数据边界：{result.error}")
    else:
        lines.extend(["## 历史统计（仅供比较）", ""])
        lines.extend(_metrics_lines("全程持有基准", result.buy_and_hold))
        lines.append("")
        lines.extend(_metrics_lines("核心与机动模拟", result.core_tactical))
        lines.extend(["", f"- 模拟状态变化记录：{result.trade_events} 次。"])
    lines.extend(
        [
            "",
            "## 风险声明",
            "",
            "历史回放会受到复权方式、缺失交易日、公开接口质量、成本和滑点假设影响；它只能帮助理解规则在过去如何表现。",
            "当前未模拟券商单笔最低佣金；机动金额较小时，历史结果可能偏乐观。",
            "股票与不同 ETF 的交易制度可能不同，本工具不替用户确认具体产品规则，也不连接券商。",
            "",
        ]
    )
    return "\n".join(lines)


def _timestamp(value: datetime) -> str:
    return value.strftime("%Y%m%d-%H%M%S-%f")


def write_private_report(
    repo_root: str | Path,
    *,
    kind: str,
    code: str,
    content: str,
    generated_at: datetime,
) -> Path:
    """在固定私有报告目录新建报告，避免覆盖同名旧报告。"""

    if kind not in {"analysis", "backtest"}:
        raise SwingReportError("报告类型不受支持")
    if _CODE_PATTERN.fullmatch(str(code)) is None:
        raise SwingReportError("报告标识格式不受支持")
    root, reports_root = _private_child(repo_root, Path(repo_root).resolve() / "data" / "private" / "reports")
    reports_root.mkdir(parents=True, exist_ok=True)
    # mkdir 后再次 resolve，防止 reports 在创建前后被替换为越界链接。
    reports_root = _private_child(root, reports_root)[1]
    if reports_root.parent.name != "private":
        raise SwingReportError("报告目录不在本机私有目录")
    base = f"{kind}-{code}-{_timestamp(generated_at)}"
    for counter in range(1000):
        suffix = "" if counter == 0 else f"-{counter:02d}"
        path = reports_root / f"{base}{suffix}.md"
        try:
            with path.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
            return _private_child(root, path)[1]
        except FileExistsError:
            continue
        except (OSError, UnicodeError) as exc:
            raise SwingReportError("私有报告无法保存") from exc
    raise SwingReportError("无法创建不覆盖旧文件的报告文件名")


def atomic_write_json(
    path: str | Path,
    document: Mapping[str, object],
    *,
    repo_root: str | Path,
) -> Path:
    """原子写入私有 JSON，并拒绝越界目标。"""

    root, target = _private_child(repo_root, Path(path))
    target.parent.mkdir(parents=True, exist_ok=True)
    # 创建父目录后重验，避免符号链接把临时文件导向私有目录之外。
    target = _private_child(root, target)[1]
    payload = json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=target.parent,
            prefix=f".{target.stem}-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        temporary = None
        return target
    except (OSError, UnicodeError, TypeError, ValueError) as exc:
        raise SwingReportError("私有运行摘要无法安全保存") from exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


__all__ = [
    "SwingReportError",
    "atomic_write_json",
    "render_analysis_report",
    "render_backtest_report",
    "write_private_report",
]
