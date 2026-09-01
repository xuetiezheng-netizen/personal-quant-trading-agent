from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from scripts import run_real
from trading_agent.domain.analysis import Recommendation, WorkflowResult
from trading_agent.domain.models import QuoteSnapshot


def _quote(*, observed_at: datetime, final: bool = True) -> QuoteSnapshot:
    return QuoteSnapshot(
        code="000001",
        name="平安银行",
        last_price=11.2,
        pct_change=1.2,
        turnover_amount=180_000_000,
        volume=12_000_000,
        observed_at=observed_at,
        open_price=11.0,
        high_price=11.5,
        low_price=10.9,
        previous_close=11.0,
        is_final_bar=final,
    )


def _snapshot(path: Path, observed_at: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "provider": "sina_free",
                "observed_at": observed_at.isoformat(),
                "page_count": 1,
                "pages": [[
                    {
                        "code": "000001",
                        "name": "平安银行",
                        "trade": "11.2",
                        "changepercent": "1.2",
                        "amount": "180000000",
                        "volume": "12000000",
                        "open": "11.0",
                        "high": "11.5",
                        "low": "10.9",
                        "settlement": "11.0",
                    }
                ]],
            }
        ),
        encoding="utf-8",
    )


def test_capture_close_rejects_outside_shanghai_weekday_window(tmp_path) -> None:
    with pytest.raises(run_real.RealRunError, match="工作日 15:01-15:15"):
        run_real.capture_close_snapshot(
            tmp_path,
            now=datetime(2026, 9, 1, 6, 59, tzinfo=UTC),
            provider_factory=lambda **_: pytest.fail("provider must not be called"),
        )


def test_capture_close_uses_sina_and_reports_new_snapshot(tmp_path) -> None:
    observed_at = datetime(2026, 9, 1, 15, 5, tzinfo=run_real.SHANGHAI)

    class FakeProvider:
        def __init__(self, *, raw_snapshot_directory: Path, now):
            self.directory = raw_snapshot_directory
            self.now = now

        def fetch_realtime_quotes(self):
            self.directory.mkdir(parents=True, exist_ok=True)
            path = self.directory / "sina-20260901T150500+0800.json"
            _snapshot(path, observed_at)
            return (_quote(observed_at=observed_at),)

    result = run_real.capture_close_snapshot(
        tmp_path,
        now=observed_at,
        provider_factory=FakeProvider,
    )

    assert result.path == (tmp_path / "data/raw_snapshots/sina-20260901T150500+0800.json").resolve()
    assert result.trade_date == date(2026, 9, 1)


def test_research_rejects_missing_snapshot(tmp_path) -> None:
    with pytest.raises(run_real.RealRunError, match="没有新浪收盘快照"):
        run_real.load_latest_final_snapshot(tmp_path, provider_factory=lambda **_: pytest.fail())


def test_research_rejects_non_final_snapshot(tmp_path) -> None:
    observed_at = datetime(2026, 9, 1, 10, 0, tzinfo=run_real.SHANGHAI)
    _snapshot(tmp_path / "data/raw_snapshots/sina-20260901T100000+0800.json", observed_at)

    with pytest.raises(run_real.RealRunError, match="不是完整收盘日线"):
        run_real.load_latest_final_snapshot(tmp_path)


def test_real_report_is_utf8_and_never_overwrites(tmp_path) -> None:
    observed_at = datetime(2026, 9, 1, 15, 5, tzinfo=run_real.SHANGHAI)
    snapshot = run_real.SnapshotInfo(
        (tmp_path / "sina.json").resolve(), observed_at, date(2026, 9, 1), 1
    )
    recommendation = Recommendation(
        code="000001",
        name="平安银行",
        total_score=72.5,
        verdict="watch_for_tail_buy",
        reasons=("收盘形态通过",),
        risks=("公开数据可能延迟",),
    )
    result = WorkflowResult(100, 5, 2, (recommendation,), ())
    generated_at = datetime(2026, 9, 1, 16, 0, tzinfo=run_real.SHANGHAI)

    first = run_real.write_real_report(result, snapshot, tmp_path, generated_at)
    second = run_real.write_real_report(result, snapshot, tmp_path, generated_at)
    content = first.read_text(encoding="utf-8")

    assert first != second
    assert first.name.startswith("real-report-20260901-160000-")
    for label in ("真实公开行情", "快照日期/数据时点", "未调用 AI 模型", "不构成投资建议"):
        assert label in content
    assert "扫描数：100" in content
    assert "观察池：5" in content
    assert "研究池：2" in content
    assert "否决数：0" in content
    assert "000001 平安银行" in content
    assert "72.50" in content
    assert "收盘形态通过" in content
    assert "公开数据可能延迟" in content


def test_no_model_workflow_does_not_load_dotenv_or_create_llm_client(monkeypatch, tmp_path) -> None:
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    monkeypatch.setattr(run_real, "_public_news_enricher", lambda _: None)

    class RecordingNoModelAgent:
        def __init__(self, *args: object, **kwargs: object) -> None:
            calls.append((args, kwargs))

    monkeypatch.setattr(run_real, "LlmResearchAgent", RecordingNoModelAgent)

    workflow = run_real._build_no_model_workflow(Path("."))

    assert workflow is not None
    assert calls == [((), {})]


def test_latest_final_snapshot_requires_provider_final_flag(tmp_path) -> None:
    observed_at = datetime(2026, 9, 1, 15, 5, tzinfo=run_real.SHANGHAI)
    _snapshot(tmp_path / "data/raw_snapshots/sina-20260901T150500+0800.json", observed_at)

    class FakeProvider:
        def __init__(self, *, snapshot_path: Path):
            self.snapshot_path = snapshot_path

        def fetch_realtime_quotes(self):
            return (_quote(observed_at=observed_at, final=False),)

    with pytest.raises(run_real.RealRunError, match="未完成日线"):
        run_real.load_latest_final_snapshot(tmp_path, provider_factory=FakeProvider)
