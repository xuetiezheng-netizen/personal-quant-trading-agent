from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from trading_agent.domain.models import DailyBar
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


class FakeProvider:
    name = "fake_public"

    def __init__(self, bars: tuple[DailyBar, ...]) -> None:
        self.bars = bars
        self.calls: list[tuple[str, str, object]] = []

    def fetch_daily_bars(self, code: str, *, asset_type: str, as_of: object = None) -> tuple[DailyBar, ...]:
        self.calls.append((code, asset_type, as_of))
        return self.bars


def _service(tmp_path: Path, bars: tuple[DailyBar, ...]) -> SwingService:
    store = PortfolioStore(tmp_path / "data" / "private" / "holdings.json")
    store.add_holding(
        {
            "code": "999999",
            "name": "虚构资产",
            "asset_type": "etf",
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
        provider=FakeProvider(bars),
        config=config,
        now=lambda: datetime(2026, 9, 1, 16, tzinfo=SHANGHAI),
    )


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
