"""Windows 新手入口：只使用新浪公开行情进行收盘快照和研究。

本入口有意不读取 ``.env``，不装配带客户端的大模型 Agent，也不包含券商
连接或下单代码。新闻仅尽力调用项目默认的公开来源；新闻失败不会替换或
回退行情来源。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from trading_agent.agents.catalyst import CatalystAgent
from trading_agent.agents.decision import DecisionAgent
from trading_agent.agents.kline_trend import KlineTrendAgent
from trading_agent.agents.llm_research import LlmResearchAgent
from trading_agent.agents.market_scanner import MarketScannerAgent
from trading_agent.agents.risk import RiskAgent
from trading_agent.agents.volume import VolumeAgent
from trading_agent.config import load_market_scanner_config
from trading_agent.config_news import NewsConfig, load_news_config
from trading_agent.config_workflow import load_application_config
from trading_agent.domain.analysis import Recommendation, WorkflowResult
from trading_agent.market_scanner.pattern_gate import PatternGate
from trading_agent.market_scanner.service import MarketScanner
from trading_agent.news.cninfo import CninfoPublicDisclosureProvider
from trading_agent.news.eastmoney import EastmoneyStockNewsProvider
from trading_agent.news.enricher import NewsEnricher
from trading_agent.orchestrator.workflow import DailyResearchWorkflow, IncompleteResearchDataError
from trading_agent.providers.eastmoney import FreeDataProviderError
from trading_agent.providers.selection import (
    completed_close_date,
    latest_sina_snapshot,
    snapshot_close_date,
)
from trading_agent.providers.sina import SinaFreeProvider, _latest_bar_is_final
from trading_agent.scheduler.window import is_close_snapshot_capture_window
from trading_agent.storage.sqlite import SqliteRecommendationRepository

try:
    SHANGHAI = ZoneInfo("Asia/Shanghai")
except ZoneInfoNotFoundError:  # Windows installations without an external tzdata package.
    SHANGHAI = timezone(timedelta(hours=8), "Asia/Shanghai")

REPORT_DIRECTORY = REPO_ROOT / "data" / "reports"
RAW_SNAPSHOT_DIRECTORY = REPO_ROOT / "data" / "raw_snapshots"
DATABASE_PATH = REPO_ROOT / "data" / "recommendations.sqlite"


class RealRunError(RuntimeError):
    """面向新手的安全边界或数据完整性错误。"""


@dataclass(frozen=True, slots=True)
class SnapshotInfo:
    path: Path
    observed_at: datetime
    trade_date: date
    quote_count: int


@dataclass(frozen=True, slots=True)
class ResearchRun:
    snapshot: SnapshotInfo
    report_path: Path
    result: WorkflowResult


def _as_shanghai(moment: datetime | None) -> datetime:
    value = moment or datetime.now().astimezone()
    if value.tzinfo is None:
        return value.replace(tzinfo=SHANGHAI)
    return value.astimezone(SHANGHAI)


def is_capture_close_window(moment: datetime) -> bool:
    """直接复用项目定义的北京时间工作日 15:01-15:15 收盘窗口。"""

    local = _as_shanghai(moment)
    return is_close_snapshot_capture_window(local)


def _require_capture_window(moment: datetime) -> datetime:
    local = _as_shanghai(moment)
    if not is_capture_close_window(local):
        raise RealRunError(
            "现在不是允许的收盘快照窗口：当前北京时间 "
            f"{local:%Y-%m-%d %H:%M:%S}。请在北京时间工作日 15:01-15:15 运行 CaptureClose。"
        )
    return local


def _snapshot_path_for(observed_at: datetime, directory: Path) -> Path:
    return directory / observed_at.strftime("sina-%Y%m%dT%H%M%S%z.json")


def capture_close_snapshot(
    repo_root: str | Path = REPO_ROOT,
    *,
    now: datetime | None = None,
    provider_factory: Callable[..., SinaFreeProvider] | None = None,
) -> SnapshotInfo:
    """抓取并确认一份新的新浪完整收盘快照，不使用缓存或其他行情源。"""

    local_now = _require_capture_window(now or datetime.now(SHANGHAI))
    root = Path(repo_root).resolve()
    raw_directory = root / "data" / "raw_snapshots"
    factory = provider_factory or SinaFreeProvider
    provider = factory(
        raw_snapshot_directory=raw_directory,
        now=lambda: local_now,
    )
    quotes = tuple(provider.fetch_realtime_quotes())
    if not quotes:
        raise RealRunError("新浪公开接口没有返回股票行情，未生成收盘快照。")
    if not all(quote.is_final_bar for quote in quotes):
        raise RealRunError("新浪返回的行情不是完整收盘日线，已拒绝保存。")
    observed_at = quotes[0].observed_at
    if any(quote.observed_at != observed_at for quote in quotes):
        raise RealRunError("新浪行情各分页的数据时点不一致，已拒绝保存。")
    snapshot_path = _snapshot_path_for(observed_at, raw_directory)
    if not snapshot_path.is_file():
        raise RealRunError("新浪行情抓取未生成预期收盘快照，未使用旧快照替代。")
    trade_date = completed_close_date(observed_at)
    return SnapshotInfo(snapshot_path.resolve(), observed_at, trade_date, len(quotes))


def _read_snapshot_observed_at(path: Path) -> datetime:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RealRunError(f"新浪快照无法解析：{path}") from exc
    if not isinstance(document, Mapping) or document.get("provider") != SinaFreeProvider.name:
        raise RealRunError("最新快照不是 sina_free 新浪快照，已拒绝研究。")
    try:
        observed_at = datetime.fromisoformat(str(document["observed_at"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise RealRunError("新浪快照缺少可识别的数据时点，已拒绝研究。") from exc
    if observed_at.tzinfo is None:
        raise RealRunError("新浪快照的数据时点缺少时区，已拒绝研究。")
    if not _latest_bar_is_final(observed_at):
        raise RealRunError(
            f"新浪快照数据时点为 {observed_at.isoformat()}，不是完整收盘日线，已拒绝研究。"
        )
    return observed_at


def load_latest_final_snapshot(
    repo_root: str | Path = REPO_ROOT,
    *,
    provider_factory: Callable[..., SinaFreeProvider] | None = None,
) -> tuple[SnapshotInfo, SinaFreeProvider]:
    """先找到、解析并验证最新新浪快照，再交给研究工作流。"""

    root = Path(repo_root).resolve()
    raw_directory = root / "data" / "raw_snapshots"
    try:
        path = latest_sina_snapshot(raw_directory).resolve()
    except FreeDataProviderError as exc:
        raise RealRunError("没有新浪收盘快照。请先在北京时间工作日 15:01-15:15 运行 CaptureClose。") from exc
    observed_at = _read_snapshot_observed_at(path)
    factory = provider_factory or SinaFreeProvider
    provider = factory(snapshot_path=path)
    quotes = tuple(provider.fetch_realtime_quotes())
    if not quotes:
        raise RealRunError("最新新浪快照没有可用行情，已拒绝研究。")
    if not all(quote.is_final_bar for quote in quotes):
        raise RealRunError("最新新浪快照含未完成日线，已拒绝研究。")
    if any(quote.observed_at != observed_at for quote in quotes):
        raise RealRunError("新浪快照元数据与行情数据时点不一致，已拒绝研究。")
    try:
        trade_date = snapshot_close_date(path)
    except FreeDataProviderError as exc:
        raise RealRunError("新浪快照无法确定交易日期，已拒绝研究。") from exc
    if trade_date > _as_shanghai(observed_at).date():
        raise RealRunError("新浪快照交易日期晚于数据时点，已拒绝研究。")
    return SnapshotInfo(path, observed_at, trade_date, len(quotes)), provider


def _public_news_enricher(repo_root: Path) -> NewsEnricher | None:
    """只装配配置中的公开新闻源；不读取 .env 或任何密钥环境变量。"""

    config_path = repo_root / "configs" / "news.yaml"
    config: NewsConfig = load_news_config(config_path)
    if not config.enabled:
        return None
    providers = []
    if config.cninfo.enabled:
        providers.append(
            CninfoPublicDisclosureProvider(
                page_size=config.cninfo.page_size,
                request_interval_seconds=config.cninfo.request_interval_seconds,
            )
        )
    if config.eastmoney_stock_news.enabled:
        providers.append(
            EastmoneyStockNewsProvider(
                request_interval_seconds=config.eastmoney_stock_news.request_interval_seconds,
            )
        )
    # Tushare is intentionally omitted, even if a future config enables it:
    # this entry point must not inspect or consume any API-key environment value.
    return NewsEnricher(config=config, providers=providers)


def _build_no_model_workflow(repo_root: Path) -> DailyResearchWorkflow:
    """组装现有研究流程，但只放入无客户端的确定性 LLM 占位器。"""

    workflow_config = load_application_config(repo_root / "configs" / "workflow.yaml")
    scanner_config = load_market_scanner_config(repo_root / "configs" / "market_scanner.yaml")
    return DailyResearchWorkflow(
        config=workflow_config,
        scanner=MarketScannerAgent(MarketScanner(scanner_config)),
        pattern_gate=PatternGate(workflow_config.pattern_gate),
        kline_trend=KlineTrendAgent(),
        volume=VolumeAgent(),
        catalyst=CatalystAgent(),
        risk=RiskAgent(),
        llm_research=LlmResearchAgent(),
        decision=DecisionAgent(workflow_config.workflow),
        news_enricher=_public_news_enricher(repo_root),
    )


@contextmanager
def _repo_working_directory(repo_root: Path) -> Iterator[None]:
    import os

    previous = Path.cwd()
    try:
        os.chdir(repo_root)
        yield
    finally:
        os.chdir(previous)


def _verdict_label(recommendation: Recommendation) -> str:
    return {
        "watch_for_tail_buy": "观察/尾盘关注",
        "not_recommended": "暂不推荐",
        "rejected": "已否决",
    }.get(recommendation.verdict, recommendation.verdict)


def render_real_report(
    result: WorkflowResult,
    snapshot: SnapshotInfo,
    generated_at: datetime | None = None,
) -> str:
    """渲染明确标注真实数据边界的中文 Markdown 报告。"""

    generated = _as_shanghai(generated_at)
    lines = [
        "# 前收盘形态候选研究报告（真实公开行情）",
        "",
        "> **真实公开行情**：本报告使用新浪 sina_free 公开行情收盘快照。",
        f"> **快照日期/数据时点**：交易日期 {snapshot.trade_date.isoformat()}；数据时点 {snapshot.observed_at.isoformat()}。",
        "> **未调用 AI 模型**：本次未读取 `.env`、未装配模型客户端、未调用 DeepSeek/Kimi 或其他 LLM。",
        "> **不构成投资建议**：结果仅用于研究和学习，不代表收益或买卖建议。",
        "> **数据边界**：公开接口可能延迟、限流、失败或字段不完整，请人工核对原始来源。",
        "",
        f"快照文件：{snapshot.path}",
        f"生成时间：{generated.isoformat(timespec='seconds')}",
        "",
        "## 结果摘要",
        "",
        f"- 扫描数：{result.scanned_count}",
        f"- 观察池：{result.observation_pool_count}",
        f"- 研究池：{result.research_pool_count}",
        f"- 否决数：{len(result.vetoed)}",
        "",
        "## 推荐候选",
        "",
    ]
    if not result.recommendations:
        lines.extend(["本次没有满足阈值的推荐候选。", ""])
    else:
        for index, recommendation in enumerate(result.recommendations, start=1):
            reasons = "；".join(recommendation.reasons) or "未提供结构化理由"
            risks = "；".join(recommendation.risks) or "未发现结构化风险"
            lines.extend(
                [
                    f"### {index}. {recommendation.code} {recommendation.name}",
                    "",
                    f"- 分数：{recommendation.total_score:.2f}",
                    f"- 结论：{_verdict_label(recommendation)}（{recommendation.verdict}）",
                    f"- 理由：{reasons}",
                    f"- 风险：{risks}",
                    "",
                ]
            )

    if result.vetoed:
        lines.extend(["## 否决候选", ""])
        for item in result.vetoed:
            lines.append(f"- {item.code} {item.name}：{'；'.join(item.risks) or '规则否决'}")
        lines.append("")
    lines.extend(
        [
            "## 使用边界",
            "",
            "本入口只做只读研究，不连接券商、不下单；免费公开源的延迟或失败可能影响结果。",
            "",
        ]
    )
    return "\n".join(lines)


def write_real_report(
    result: WorkflowResult,
    snapshot: SnapshotInfo,
    report_directory: str | Path = REPORT_DIRECTORY,
    generated_at: datetime | None = None,
) -> Path:
    """以独占创建方式写 UTF-8 报告，永不覆盖同名旧文件。"""

    directory = Path(report_directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    generated = _as_shanghai(generated_at)
    timestamp = generated.strftime("%Y%m%d-%H%M%S-%f")
    content = render_real_report(result, snapshot, generated_at=generated)
    for counter in range(1000):
        suffix = "" if counter == 0 else f"-{counter:02d}"
        path = directory / f"real-report-{timestamp}{suffix}.md"
        try:
            with path.open("x", encoding="utf-8", newline="\n") as report_file:
                report_file.write(content)
            return path.resolve()
        except FileExistsError:
            continue
    raise RealRunError("无法创建不覆盖旧文件的真实行情报告文件名。")


def run_research(
    repo_root: str | Path = REPO_ROOT,
    *,
    generated_at: datetime | None = None,
    provider_factory: Callable[..., SinaFreeProvider] | None = None,
    workflow_factory: Callable[[Path], DailyResearchWorkflow] | None = None,
) -> ResearchRun:
    """读取最新完整新浪快照，执行规则研究，保存报告和 SQLite 记录。"""

    root = Path(repo_root).resolve()
    snapshot, provider = load_latest_final_snapshot(root, provider_factory=provider_factory)
    with _repo_working_directory(root):
        workflow = (workflow_factory or _build_no_model_workflow)(root)
        result = workflow.run(provider)
    run_at = _as_shanghai(generated_at)
    database_path = root / "data" / "recommendations.sqlite"
    SqliteRecommendationRepository(database_path).save(result, run_at=run_at)
    report_path = write_real_report(
        result,
        snapshot,
        report_directory=root / "data" / "reports",
        generated_at=run_at,
    )
    return ResearchRun(snapshot=snapshot, report_path=report_path, result=result)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="只读新浪公开行情研究入口")
    parser.add_argument("--mode", choices=("CaptureClose", "Research"), required=True)
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.mode == "CaptureClose":
            snapshot = capture_close_snapshot(args.repo_root)
            print("新浪收盘快照已保存（只读、未交易）：")
            print(f"- 快照绝对路径：{snapshot.path}")
            print(f"- 快照交易日期：{snapshot.trade_date.isoformat()}")
            print(f"- 数据时点：{snapshot.observed_at.isoformat()}")
            print(f"- 股票数量：{snapshot.quote_count}")
        else:
            research = run_research(args.repo_root)
            print("真实公开行情研究报告已生成（未调用 AI、未交易）：")
            print(f"- 报告绝对路径：{research.report_path}")
            print(f"- 快照交易日期：{research.snapshot.trade_date.isoformat()}")
            print(f"- 数据时点：{research.snapshot.observed_at.isoformat()}")
            print(f"- SQLite 记录：{Path(args.repo_root).resolve() / 'data' / 'recommendations.sqlite'}")
    except (FreeDataProviderError, IncompleteResearchDataError, OSError, RealRunError, ValueError) as exc:
        print(f"真实行情运行失败：{exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
