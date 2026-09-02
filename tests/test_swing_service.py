from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from trading_agent.domain.models import DailyBar
from trading_agent.swing.data import HistoryBar, HistoryData
from trading_agent.swing.history_providers import FailoverHistoryProvider
from trading_agent.swing.models import SwingConfig, SwingState
from trading_agent.swing.portfolio import Holding, PortfolioStore
from trading_agent.swing.reports import SwingReportError, write_private_report
from trading_agent.swing.service import (
    SwingService,
    SwingServiceError,
    load_latest_results,
)

SHANGHAI = timezone(timedelta(hours=8))


def _bars(count: int) -> tuple[DailyBar, ...]:
    origin = datetime(2025, 1, 1, 15, tzinfo=SHANGHAI)
    result: list[DailyBar] = []
    for index in range(count):
        close = 10.0 + ((index % 17) - 8) * 0.08 + index * 0.002
        open_price = close - 0.05 if index % 3 else close + 0.05
        result.append(
            DailyBar(
                trade_date=origin + timedelta(days=index),
                open_price=open_price,
                high_price=max(open_price, close) + 0.15,
                low_price=min(open_price, close) - 0.15,
                close_price=close,
                volume=1000.0 + (index % 5) * 10.0,
                turnover_amount=close * 1000.0,
            )
        )
    return tuple(result)


def _history_data(
    source: str,
    bars: tuple[DailyBar, ...],
    *,
    code: str = "999999",
    asset_type: str = "etf",
    exchange: str = "sz",
    source_attempts: tuple[dict[str, str], ...] = (),
) -> HistoryData:
    history_bars = tuple(
        HistoryBar(
            date=bar.trade_date.date(),
            open=bar.open_price,
            high=bar.high_price,
            low=bar.low_price,
            close=bar.close_price,
            volume=bar.volume,
            amount=bar.turnover_amount,
        )
        for bar in bars
    )
    return HistoryData(
        code=code,
        asset_type=asset_type,  # type: ignore[arg-type]
        exchange=exchange,  # type: ignore[arg-type]
        secid=f"0.{code}",
        bars=history_bars,
        source=source,
        source_url="https://example.invalid/history",
        adjustment="qfq",
        requested_as_of=datetime(2026, 9, 1, 16, tzinfo=SHANGHAI),
        requested_start=history_bars[0].date,
        requested_end=history_bars[-1].date,
        completed_through=history_bars[-1].date,
        fetched_at=datetime(2026, 9, 1, 16, tzinfo=SHANGHAI),
        raw_bar_count=len(history_bars),
        dropped_bar_count=0,
        complete=True,
        volume_unit="shares",
        amount_unit="CNY",
        source_attempts=source_attempts,  # type: ignore[arg-type]
    )


class FakeProvider:
    name = "fake_public"

    def __init__(self, bars: tuple[DailyBar, ...]) -> None:
        self.bars = bars
        self.calls: list[tuple[str, str, object]] = []

    def fetch_daily_bars(self, code: str, *, asset_type: str, as_of: object = None) -> tuple[DailyBar, ...]:
        self.calls.append((code, asset_type, as_of))
        return self.bars


class _FailingHistoryProvider:
    def __init__(self, name: str, error: BaseException) -> None:
        self.name = name
        self.error = error

    def fetch_daily_bars(self, code: str, **kwargs: object) -> object:
        raise self.error


class _FixedHistoryProvider:
    def __init__(self, name: str, result: HistoryData) -> None:
        self.name = name
        self.result = result

    def fetch_daily_bars(self, code: str, **kwargs: object) -> HistoryData:
        return self.result


def _service(
    tmp_path: Path,
    bars: tuple[DailyBar, ...],
    *,
    provider: object | None = None,
    code: str = "999999",
    asset_type: str = "etf",
) -> SwingService:
    store = PortfolioStore(tmp_path / "data" / "private" / "holdings.json")
    store.add_holding(
        {
            "code": code,
            "name": "虚构资产",
            "asset_type": asset_type,
            "quantity": 100,
            "avg_cost_cny": 10.0,
        },
        expected_revision=0,
    )
    config = SwingConfig(
        price_position_window=30,
        trend_fast_window=10,
        trend_slow_window=20,
        rsi_window=14,
        atr_window=14,
        bollinger_window=20,
        relative_volume_window=20,
        min_history_bars=120,
        min_holding_bars=10,
        action_cooldown_bars=10,
    )
    return SwingService(
        tmp_path,
        portfolio_store=store,
        provider=provider or FakeProvider(bars),
        config=config,
        now=lambda: datetime(2026, 9, 1, 16, tzinfo=SHANGHAI),
    )


def _empty_service(tmp_path: Path) -> SwingService:
    store = PortfolioStore(tmp_path / "data" / "private" / "holdings.json")
    return SwingService(
        tmp_path,
        portfolio_store=store,
        now=lambda: datetime(2026, 9, 1, 16, tzinfo=SHANGHAI),
    )


def test_default_provider_order_without_token(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)

    service = _empty_service(tmp_path)

    assert [provider.name for provider in service.provider.providers] == [
        "eastmoney",
        "tencent",
        "baostock",
    ]
    assert "tushare" not in repr(service.provider).lower()


def test_default_provider_adds_tushare_only_for_non_empty_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    token = "secret-token"
    monkeypatch.setenv("TUSHARE_TOKEN", token)

    service = _empty_service(tmp_path)

    assert [provider.name for provider in service.provider.providers] == [
        "eastmoney",
        "tushare",
        "tencent",
        "baostock",
    ]
    assert token not in repr(service.provider)


def test_explicit_provider_injection_is_preserved(tmp_path: Path) -> None:
    provider = FakeProvider(_bars(2))

    service = _service(tmp_path, _bars(2), provider=provider)

    assert service.provider is provider


def test_unapproved_attempt_reason_is_not_persisted(tmp_path: Path) -> None:
    malicious_reason = "provider_error_token"
    history = _history_data(
        "tushare",
        _bars(150),
        source_attempts=(
            {
                "source": "tushare",
                "status": "failed",
                "reason_code": malicious_reason,
            },
        ),
    )
    service = _service(
        tmp_path,
        _bars(150),
        provider=_FixedHistoryProvider("tushare", history),
    )

    result = service.analyze("999999")
    latest = json.loads(service.latest_results_path.read_text(encoding="utf-8"))
    report = (tmp_path / result.report_path).read_text(encoding="utf-8")  # type: ignore[arg-type]

    assert result.source_attempts == ()
    assert result.as_dict()["source_attempts"] == []
    assert latest["results"][0]["source_attempts"] == []
    assert malicious_reason not in repr(result)
    assert malicious_reason not in report
    assert "Tushare Pro" in report


def test_provider_switch_attempts_reach_result_latest_and_report(tmp_path: Path) -> None:
    bars = _bars(150)
    primary = _FailingHistoryProvider("eastmoney", RuntimeError("secret-token-and-private-data"))
    secondary = _FixedHistoryProvider(
        "tencent", _history_data("tencent", bars, code="510300", exchange="sh")
    )
    provider = FailoverHistoryProvider([primary, secondary])
    service = _service(tmp_path, bars, provider=provider, code="510300")

    result = service.analyze("510300")
    payload = result.as_dict()
    latest = json.loads(service.latest_results_path.read_text(encoding="utf-8"))
    report = (tmp_path / result.report_path).read_text(encoding="utf-8")  # type: ignore[arg-type]

    assert result.status == "ok"
    assert result.data_source == "tencent"
    assert payload["source_attempts"] == [
        {"source": "eastmoney", "status": "failed", "reason_code": "provider_error"},
        {"source": "tencent", "status": "success", "reason_code": "ok"},
    ]
    assert latest["results"][0]["source_attempts"] == payload["source_attempts"]
    assert "腾讯" in report
    assert "已自动切换" in report
    assert "provider_error" not in report
    assert "secret-token" not in repr(result)


def test_all_failures_are_safe_and_expose_only_public_attempts(tmp_path: Path) -> None:
    token = "very-secret-token"
    provider = FailoverHistoryProvider(
        [
            _FailingHistoryProvider("eastmoney", RuntimeError(f"upstream token={token}")),
            _FailingHistoryProvider("unknown-private-source", RuntimeError("private error")),
            _FailingHistoryProvider("tencent", RuntimeError("temporary failure")),
        ]
    )
    service = _service(tmp_path, _bars(150), provider=provider, code="510300")

    analysis = service.analyze("510300")
    backtest = service.backtest("510300")
    latest = json.loads(service.latest_results_path.read_text(encoding="utf-8"))
    analysis_report = (tmp_path / analysis.report_path).read_text(encoding="utf-8")  # type: ignore[arg-type]
    backtest_report = (tmp_path / backtest.report_path).read_text(encoding="utf-8")  # type: ignore[arg-type]

    expected = [
        {"source": "eastmoney", "status": "failed", "reason_code": "provider_error"},
        {
            "source": "unknown-private-source",
            "status": "failed",
            "reason_code": "provider_error",
        },
        {"source": "tencent", "status": "failed", "reason_code": "provider_error"},
    ]
    assert analysis.status == "error"
    assert backtest.status == "error"
    assert analysis.source_attempts == tuple(expected)
    assert backtest.source_attempts == tuple(expected)
    assert all(item["source_attempts"] == expected for item in latest["results"])
    for text in (repr(analysis), repr(backtest), analysis_report, backtest_report):
        assert token not in text
    for text in (analysis_report, backtest_report):
        assert "provider_error" not in text
        assert "unknown-private-source" not in text
        assert "自动线路均不可用（尝试过：东方财富、腾讯）" in text


def test_analyze_writes_private_beginner_report_and_latest_contract(tmp_path: Path) -> None:
    service = _service(tmp_path, _bars(150))

    result = service.analyze("999999")

    assert result.status == "ok"
    assert result.state in {
        SwingState.LOW_WATCH,
        SwingState.BOTTOM_CONFIRMED,
        SwingState.NEUTRAL,
        SwingState.HIGH_WATCH,
        SwingState.TOP_CONFIRMED,
    }
    assert result.report_path is not None
    report = (tmp_path / result.report_path).read_text(encoding="utf-8")
    assert "策略版本" in report
    assert "数据截止" in report
    assert "未使用的信息" in report
    assert "买入" not in report
    assert "卖出" not in report
    assert "加仓" not in report
    assert "减仓" not in report

    latest = json.loads(service.latest_results_path.read_text(encoding="utf-8"))
    assert latest["schema_version"] == 1
    assert latest["results"][0]["code"] == "999999"
    assert latest["results"][0]["report_path"] == result.report_path
    assert not Path(latest["results"][0]["report_path"]).is_absolute()
    assert service.run_summary_path.is_file()
    assert load_latest_results(tmp_path)["results"]


def test_backtest_reports_assumptions_and_preserves_core_boundary(tmp_path: Path) -> None:
    service = _service(tmp_path, _bars(180))

    result = service.backtest("999999")

    assert result.status == "ok"
    assert result.has_performance
    assert result.static_core_cash is not None
    assert result.core_weight == pytest.approx(0.8)
    assert result.tactical_weight == pytest.approx(0.2)
    assert result.costs.commission_bps == pytest.approx(3.0)
    assert result.costs.slippage_bps == pytest.approx(5.0)
    assert result.costs.sell_tax_bps == pytest.approx(0.0)
    payload = result.as_dict()
    assert payload["static_core_cash"] is not None
    assert payload["assumptions"]["warmup_excluded"] is True
    assert payload["assumptions"]["drawdown_basis"] == "daily_close"
    assert payload["core_tactical"]["market_exposure"] >= result.core_weight
    assert payload["robustness"]["status"] == "ok"
    assert payload["robustness"]["direction_total"] == 4

    latest = service.load_latest_results()
    latest_backtest = next(
        item for item in latest["results"] if item["mode"] == "backtest"
    )
    assert latest_backtest["static_core_cash"] == payload["static_core_cash"]
    assert latest_backtest["deferred_count"] == result.deferred_count
    assert latest_backtest["robustness"] == payload["robustness"]

    assert result.report_path is not None
    report = (tmp_path / result.report_path).read_text(encoding="utf-8")
    assert "20%" in report
    assert "核心仓比例 80% 全程保持不变" in report
    assert "下一根日线开盘" in report
    assert "手续费" in report
    assert "静态核心与现金基准" in report
    assert "预热" in report
    assert "收盘口径" in report
    assert "固定敏感性检查" in report
    assert "不能证明未来有效" in report
    assert "不是用户真实收益" in report
    assert "买入" not in report
    assert "卖出" not in report


def test_default_public_cost_assumption_distinguishes_stock_tax(tmp_path: Path) -> None:
    service = _service(tmp_path, _bars(150))
    stock = Holding(
        code="999997",
        name="虚构股票",
        asset_type="stock",
        quantity=100,
        avg_cost_cny=10.0,
    )

    assert service._costs_for(stock).sell_tax_bps == pytest.approx(5.0)


def test_backtest_uses_editable_holding_simulation_ratio(tmp_path: Path) -> None:
    service = _service(tmp_path, _bars(150))
    current = service.portfolio_store.load()
    service.portfolio_store.update_holding(
        "999999",
        {"tactical_ratio": 0.35},
        expected_revision=current.revision,
    )

    result = service.backtest("999999")

    assert result.tactical_weight == pytest.approx(0.35)
    assert result.core_weight == pytest.approx(0.65)


def test_insufficient_history_does_not_create_return_conclusion(tmp_path: Path) -> None:
    service = _service(tmp_path, _bars(119))

    analysis = service.analyze("999999")
    backtest = service.backtest("999999")

    assert analysis.state is SwingState.DATA_INSUFFICIENT
    assert analysis.status == "data_insufficient"
    assert backtest.status == "data_insufficient"
    assert backtest.buy_and_hold is None
    assert backtest.static_core_cash is None
    assert backtest.core_tactical is None
    assert backtest.trade_events == 0
    assert backtest.report_path is not None
    text = (tmp_path / backtest.report_path).read_text(encoding="utf-8")
    assert "不生成任何收益结论" in text
    assert "总收益（历史模拟）" not in text


def test_all_updates_both_modes_and_only_calls_public_provider(tmp_path: Path) -> None:
    service = _service(tmp_path, _bars(150))

    run = service.run("all")

    assert len(run.results) == 2
    assert {item.as_dict()["mode"] for item in run.results} == {"analyze", "backtest"}
    assert len(service.provider.calls) == 2  # type: ignore[attr-defined]
    assert all(call[0] == "999999" for call in service.provider.calls)  # type: ignore[attr-defined]
    assert all((tmp_path / path).is_file() for path in run.report_paths)


def test_service_rejects_code_path_traversal_without_writing_outside_private(tmp_path: Path) -> None:
    service = _service(tmp_path, _bars(150))

    with pytest.raises(SwingServiceError):
        service.run("analyze", code="../999999")
    assert not list(tmp_path.parent.glob("analysis-*.md"))


def test_report_writer_rejects_path_like_code_and_outside_target(tmp_path: Path) -> None:
    generated = datetime(2026, 9, 1, 16, tzinfo=SHANGHAI)
    with pytest.raises(SwingReportError):
        write_private_report(
            tmp_path,
            kind="analysis",
            code="../999999",
            content="synthetic",
            generated_at=generated,
        )
    with pytest.raises(SwingReportError):
        from trading_agent.swing.reports import atomic_write_json

        atomic_write_json(
            tmp_path / "outside.json",
            {"schema_version": 1},
            repo_root=tmp_path,
        )
