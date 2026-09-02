from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from trading_agent.domain.models import DailyBar
from trading_agent.swing.models import SwingConfig
from trading_agent.swing.portfolio import PortfolioStore
from trading_agent.swing.service import SwingService


def _bars(count: int = 125) -> tuple[DailyBar, ...]:
    origin = datetime(2025, 1, 1, 15, tzinfo=UTC)
    result: list[DailyBar] = []
    for index in range(count):
        close = 12.0 + (index % 9) * 0.03
        result.append(
            DailyBar(
                trade_date=origin + timedelta(days=index),
                open_price=close,
                high_price=close + 0.1,
                low_price=close - 0.1,
                close_price=close,
                volume=100.0,
                turnover_amount=1200.0,
            )
        )
    return tuple(result)


class FakeProvider:
    name = "fake_public"

    def fetch_daily_bars(self, code: str, *, asset_type: str, as_of: object = None):
        return _bars()


def _service(tmp_path: Path) -> SwingService:
    store = PortfolioStore(tmp_path / "data" / "private" / "holdings.json")
    store.add_holding(
        {
            "code": "999998",
            "name": "虚构代码",
            "asset_type": "stock",
            "quantity": 100,
            "avg_cost_cny": 12.0,
        },
        expected_revision=0,
    )
    config = SwingConfig(
        price_position_window=30,
        trend_fast_window=10,
        trend_slow_window=20,
        min_history_bars=120,
    )
    return SwingService(
        tmp_path,
        portfolio_store=store,
        provider=FakeProvider(),
        config=config,
        now=lambda: datetime(2026, 9, 1, 16, tzinfo=UTC),
    )


def test_cli_all_uses_private_relative_output_and_no_error_path(
    tmp_path: Path, capsys
) -> None:
    from scripts import run_swing

    service = _service(tmp_path)
    exit_code = run_swing.main(
        ["all", "--repo-root", str(tmp_path)],
        service_factory=lambda _root: service,
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "共处理 2 个结果" in captured.out
    assert str(tmp_path) not in captured.err
    assert str(tmp_path) not in captured.out
    assert service.latest_results_path.is_file()


def test_cli_failure_is_generic_and_does_not_echo_personal_path(tmp_path: Path, capsys) -> None:
    from scripts import run_swing

    def broken(_root: Path) -> SwingService:
        raise RuntimeError(str(tmp_path / "private" / "personal-secret.json"))

    exit_code = run_swing.main(
        ["analyze", "--repo-root", str(tmp_path)],
        service_factory=broken,
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "持仓波段运行失败" in captured.err
    assert str(tmp_path) not in captured.err
    assert "personal-secret" not in captured.err


def test_parser_accepts_beginner_modes() -> None:
    from scripts.run_swing import build_parser

    assert build_parser().parse_args(["analyze"]).mode == "analyze"
    assert build_parser().parse_args(["backtest"]).mode == "backtest"
    assert build_parser().parse_args(["all"]).mode == "all"


class _CaptureService:
    def __init__(self, _repo_root: Path, captured: list[dict[str, str | None]]) -> None:
        captured.append(
            {
                "tushare": os.environ.get("TUSHARE_TOKEN"),
                "deepseek": os.environ.get("DEEPSEEK_API_KEY"),
            }
        )


def test_default_service_prefers_process_token_and_does_not_read_other_dotenv_secrets(
    tmp_path: Path, monkeypatch
) -> None:
    from scripts import run_swing

    monkeypatch.setenv("TUSHARE_TOKEN", "process-token")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    (tmp_path / ".env").write_text(
        "TUSHARE_TOKEN=repo-token\nDEEPSEEK_API_KEY=dotenv-model-secret\n",
        encoding="utf-8",
    )
    captured: list[dict[str, str | None]] = []
    monkeypatch.setattr(
        run_swing,
        "SwingService",
        lambda root: _CaptureService(root, captured),
    )

    run_swing._build_service(tmp_path)

    assert captured == [{"tushare": "process-token", "deepseek": None}]
    assert os.environ["TUSHARE_TOKEN"] == "process-token"
    assert os.environ.get("DEEPSEEK_API_KEY") is None


def test_default_service_falls_back_to_repo_dotenv_and_restores_environment(
    tmp_path: Path, monkeypatch
) -> None:
    from scripts import run_swing

    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    (tmp_path / ".env").write_text(
        "TUSHARE_TOKEN=repo-token\nDEEPSEEK_API_KEY=dotenv-model-secret\n",
        encoding="utf-8",
    )
    captured: list[dict[str, str | None]] = []
    monkeypatch.setattr(
        run_swing,
        "SwingService",
        lambda root: _CaptureService(root, captured),
    )

    run_swing._build_service(tmp_path)

    assert captured == [{"tushare": "repo-token", "deepseek": None}]
    assert os.environ.get("TUSHARE_TOKEN") is None
    assert os.environ.get("DEEPSEEK_API_KEY") is None


def test_blank_tokens_do_not_enable_optional_provider_or_print_secrets(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    from scripts import run_swing

    monkeypatch.setenv("TUSHARE_TOKEN", "   ")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    (tmp_path / ".env").write_text(
        "TUSHARE_TOKEN=   \nDEEPSEEK_API_KEY=dotenv-model-secret\n",
        encoding="utf-8",
    )
    captured: list[dict[str, str | None]] = []
    monkeypatch.setattr(
        run_swing,
        "SwingService",
        lambda root: _CaptureService(root, captured),
    )

    run_swing._build_service(tmp_path)

    assert captured == [{"tushare": "   ", "deepseek": None}]
    assert os.environ["TUSHARE_TOKEN"] == "   "
    output = capsys.readouterr()
    assert "dotenv-model-secret" not in output.out + output.err


def test_service_factory_injection_does_not_load_repo_dotenv(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    from scripts import run_swing

    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    (tmp_path / ".env").write_text(
        "TUSHARE_TOKEN=repo-token\nDEEPSEEK_API_KEY=dotenv-model-secret\n",
        encoding="utf-8",
    )
    service = _service(tmp_path)

    exit_code = run_swing.main(
        ["analyze", "--repo-root", str(tmp_path)],
        service_factory=lambda _root: service,
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert os.environ.get("TUSHARE_TOKEN") is None
    assert os.environ.get("DEEPSEEK_API_KEY") is None
    assert "repo-token" not in captured.out + captured.err
    assert "dotenv-model-secret" not in captured.out + captured.err
