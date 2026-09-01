from __future__ import annotations

from datetime import UTC, datetime

from scripts import run_demo
from trading_agent.providers.demo import DemoMarketDataProvider


def test_demo_workflow_is_offline_and_has_expected_funnel() -> None:
    result = run_demo._run_demo()

    assert result.scanned_count == 6
    assert result.observation_pool_count == 2
    assert result.research_pool_count == 2
    assert {item.code for item in result.recommendations} == {"300001", "000001"}
    assert all("AI 辅助分析尚未启用" in item.reasons for item in result.recommendations)


def test_report_is_utf8_timestamped_and_does_not_overwrite(tmp_path) -> None:
    result = run_demo._run_demo()
    generated_at = datetime(2026, 9, 1, 10, 20, 30, tzinfo=UTC)

    first = run_demo.write_demo_report(result, tmp_path, generated_at=generated_at)
    second = run_demo.write_demo_report(result, tmp_path, generated_at=generated_at)
    content = first.read_text(encoding="utf-8")

    assert first != second
    assert first.name.startswith("demo-report-20260901-102030-")
    assert "内置演示数据" in content
    assert "未调用 AI 模型" in content
    assert "不构成投资建议" in content
    assert "扫描数：6" in content
    assert "观察池：2" in content
    assert "研究池：2" in content
    assert "否决数：0" in content
    assert "300001 特锐德" in content
    assert "000001 平安银行" in content
    assert "今日真实选股" not in content


def test_demo_provider_is_the_only_provider_used(monkeypatch) -> None:
    calls: list[str] = []

    original = DemoMarketDataProvider.fetch_realtime_quotes

    def record(self: DemoMarketDataProvider):
        calls.append(self.name)
        return original(self)

    monkeypatch.setattr(DemoMarketDataProvider, "fetch_realtime_quotes", record)
    result = run_demo._run_demo()

    assert result.scanned_count == 6
    assert calls == ["demo"]
