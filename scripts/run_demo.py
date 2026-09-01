"""运行项目内置的、完全离线的选股流程演示。"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
REPORT_DIRECTORY = REPO_ROOT / "data" / "demo_reports"

# The checkout uses a src layout.  Keeping this fallback makes the script work
# even when the editable package installation is not available in a moved copy.
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from trading_agent import bootstrap
from trading_agent.agents.llm_research import LlmResearchAgent
from trading_agent.bootstrap import build_daily_research_workflow
from trading_agent.domain.analysis import Recommendation, WorkflowResult
from trading_agent.providers.demo import DemoMarketDataProvider


def _blocked_load_dotenv(*_args: object, **_kwargs: object) -> bool:
    """Guard the demo from reading a user's local .env file."""

    return False


def _offline_llm_agent(*_args: object, **_kwargs: object) -> LlmResearchAgent:
    """Build the deterministic no-model agent required by the demo."""

    return LlmResearchAgent()


@contextmanager
def _offline_workflow() -> Iterator[object]:
    """Build the regular workflow while enforcing the demo's offline boundary."""

    # build_daily_research_workflow is intentionally still the project's normal
    # assembler.  These temporary guards prevent optional bootstrap behavior from
    # reading .env or constructing a network LLM client for this demo process.
    with (
        patch.object(bootstrap, "load_dotenv", _blocked_load_dotenv),
        patch.object(bootstrap, "build_llm_research_agent", _offline_llm_agent),
    ):
        yield build_daily_research_workflow(news_config_path=None)


def _run_demo() -> WorkflowResult:
    """Run only DemoMarketDataProvider from the repository root."""

    previous_directory = Path.cwd()
    try:
        os.chdir(REPO_ROOT)
        with _offline_workflow() as workflow:
            return workflow.run(DemoMarketDataProvider())  # type: ignore[union-attr]
    finally:
        os.chdir(previous_directory)


def _verdict_label(recommendation: Recommendation) -> str:
    labels = {
        "watch_for_tail_buy": "观察/尾盘关注",
        "not_recommended": "暂不推荐",
        "rejected": "已否决",
    }
    return labels.get(recommendation.verdict, recommendation.verdict)


def render_demo_report(result: WorkflowResult, generated_at: datetime | None = None) -> str:
    """Render an explicit, beginner-friendly report from the workflow result."""

    timestamp = (generated_at or datetime.now().astimezone()).isoformat(timespec="seconds")
    lines = [
        "# 前收盘形态候选研究报告（离线演示）",
        "",
        "> **内置演示数据**：本报告使用项目内置的固定样本，不读取实时行情。",
        "> **未调用 AI 模型**：本次只运行确定性规则和内置演示数据。",
        "> **不构成投资建议**：结果仅用于熟悉项目流程和报告格式。",
        "",
        f"生成时间：{timestamp}",
        "数据源：DemoMarketDataProvider（内置演示数据）",
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
        lines.append("本次没有满足演示阈值的候选。")
        lines.append("")
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
        lines.extend(
            f"- {item.code} {item.name}：{'；'.join(item.risks) or '规则否决'}"
            for item in result.vetoed
        )
        lines.append("")

    lines.extend(
        [
            "## 使用边界",
            "",
            "这是离线演示报告，不代表实时行情或未来收益；接入真实数据前请先理解筛选规则并进行独立回测。",
            "",
        ]
    )
    return "\n".join(lines)


def write_demo_report(
    result: WorkflowResult,
    report_directory: Path | None = None,
    generated_at: datetime | None = None,
) -> Path:
    """Write a timestamped UTF-8 report without overwriting an existing file."""

    directory = (report_directory or REPORT_DIRECTORY).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    report_time = generated_at or datetime.now().astimezone()
    timestamp = report_time.strftime("%Y%m%d-%H%M%S-%f")
    content = render_demo_report(result, generated_at=report_time)

    counter = 0
    while True:
        suffix = "" if counter == 0 else f"-{counter:02d}"
        path = directory / f"demo-report-{timestamp}{suffix}.md"
        try:
            with path.open("x", encoding="utf-8", newline="\n") as report_file:
                report_file.write(content)
            return path.resolve()
        except FileExistsError:
            counter += 1
            if counter > 999:
                raise RuntimeError("无法创建不覆盖旧文件的演示报告文件名")


def main() -> int:
    try:
        result = _run_demo()
        report_path = write_demo_report(result)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"离线演示运行失败：{exc}", file=sys.stderr)
        return 1

    print(f"演示报告已生成：{report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
