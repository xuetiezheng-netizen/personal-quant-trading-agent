from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest

from trading_agent.swing import _adata_worker
from trading_agent.swing.data import HistoryBar, HistoryData, HistoryDataError
from trading_agent.swing.history_providers import (
    ADataEtfHistoryProvider,
    ADataHistoryError,
    BaoStockHistoryProvider,
    FailoverHistoryProvider,
    HistoryFailoverError,
    HistoryUnsupportedError,
    TencentHistoryProvider,
    build_default_history_provider,
)

_SHANGHAI_TZ = timezone(timedelta(hours=8))


def _rows() -> list[dict[str, object]]:
    return [
        {
            "date": "2026-08-31",
            "open": 10,
            "high": 10.5,
            "low": 9.8,
            "close": 10.2,
            "volume": 1000,
            "amount": 20000,
        },
        {
            "date": "2026-09-01",
            "open": 10.2,
            "high": 10.8,
            "low": 10,
            "close": 10.6,
            "volume": 1200,
            "amount": 22000,
        },
    ]


def _history(
    *,
    source: str = "fake",
    volume_unit: str = "source_native",
    amount_unit: str = "CNY",
) -> HistoryData:
    bars = tuple(
        HistoryBar(
            date=date(2026, 8, 31) if index == 0 else date(2026, 9, 1),
            open=10.0 + index,
            high=10.5 + index,
            low=9.8 + index,
            close=10.2 + index,
            volume=1000,
            amount=20000,
        )
        for index in range(2)
    )
    return HistoryData(
        code="600001",
        asset_type="stock",
        exchange="sh",
        secid="1.600001",
        bars=bars,
        source=source,
        source_url="https://example.invalid/history",
        adjustment="qfq",
        requested_as_of=date(2026, 9, 1),
        requested_start=date(2026, 8, 31),
        requested_end=date(2026, 9, 1),
        completed_through=date(2026, 9, 1),
        fetched_at=datetime(2026, 9, 2, tzinfo=_SHANGHAI_TZ),
        raw_bar_count=2,
        dropped_bar_count=0,
        volume_unit=volume_unit,
        amount_unit=amount_unit,
    )


class _FakeProvider:
    def __init__(self, name: str, result: object) -> None:
        self.name = name
        self.result = result
        self.calls: list[dict[str, object]] = []

    def fetch_daily_bars(self, code: str, **kwargs: object) -> object:
        self.calls.append({"code": code, **kwargs})
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def test_history_data_source_attempts_default_empty_and_failover_does_not_mix() -> None:
    original = _history(source="primary")
    primary = _FakeProvider("primary", RuntimeError("secret-token-and-private-data"))
    secondary = _FakeProvider("secondary", replace(original, source="secondary"))

    result = FailoverHistoryProvider([primary, secondary]).fetch_daily_bars(
        "600001", asset_type="stock", as_of=date(2026, 9, 1)
    )

    assert original.source_attempts == ()
    assert result.source == "secondary"
    assert result.bars == secondary.result.bars
    assert result.source_attempts[0].source == "primary"
    assert result.source_attempts[0].status == "failed"
    assert result.source_attempts[0].reason_code == "provider_error"
    assert result.source_attempts[1].as_dict() == {
        "source": "secondary",
        "status": "success",
        "reason_code": "ok",
    }
    assert "secret-token" not in repr(result.source_attempts)


def test_failover_switches_on_unqualified_future_result_and_all_failures_are_sanitized() -> None:
    invalid = replace(
        _history(source="bad"),
        bars=(_history().bars[0],),
        completed_through=date(2026, 9, 1),
    )
    good = replace(_history(source="good"), requested_end=date(2026, 9, 1))
    first = _FakeProvider("bad", invalid)
    second = _FakeProvider("good", good)
    result = FailoverHistoryProvider([first, second]).fetch_daily_bars(
        "600001", asset_type="stock", as_of=date(2026, 9, 1)
    )
    assert result.source == "good"
    assert result.source_attempts[0].reason_code == "invalid_result"

    failing = _FakeProvider("private-source", ValueError("https://private/?token=secret"))
    with pytest.raises(HistoryFailoverError) as error_info:
        FailoverHistoryProvider([failing]).fetch_daily_bars(
            "600001", asset_type="stock", as_of=date(2026, 9, 1)
        )
    error = error_info.value
    assert error.attempts[0].source == "private-source"
    assert error.attempts[0].reason_code == "provider_error"
    assert "private" not in str(error)
    assert "secret" not in repr(error.attempts)


@pytest.mark.parametrize(
    "bad_history",
    [
        _history(source="wrong-source"),
        _history(volume_unit="hands"),
        _history(amount_unit="thousand_cny"),
    ],
)
def test_failover_rejects_forged_source_and_units(bad_history: HistoryData) -> None:
    provider = _FakeProvider("primary", bad_history)
    with pytest.raises(HistoryFailoverError) as error_info:
        FailoverHistoryProvider([provider]).fetch_daily_bars(
            "600001", asset_type="stock", as_of=date(2026, 9, 1)
        )
    assert error_info.value.attempts[0].reason_code == "invalid_result"


def test_tencent_parses_english_fields_and_rejects_etf() -> None:
    calls: list[dict[str, object]] = []

    def fetcher(**kwargs: object) -> list[dict[str, object]]:
        calls.append(kwargs)
        return _rows() + [{**_rows()[-1], "date": "2026-09-03", "close": 99}]

    provider = TencentHistoryProvider(
        frame_fetcher=fetcher,
        now=lambda: datetime(2026, 9, 2, 16, 0, tzinfo=_SHANGHAI_TZ),
    )
    result = provider.fetch_daily_bars(
        "600001", asset_type="stock", as_of=date(2026, 9, 1), adjustment="hfq"
    )

    assert [bar.date for bar in result.bars] == [date(2026, 8, 31), date(2026, 9, 1)]
    assert result.volume_unit == "shares"
    assert result.amount_unit == "CNY"
    assert calls[0]["symbol"] == "600001"
    assert calls[0]["adjust"] == "hfq"
    with pytest.raises(HistoryUnsupportedError):
        provider.fetch_daily_bars("510300", asset_type="etf", as_of=date(2026, 9, 1))


def _adata_rows() -> list[dict[str, object]]:
    return [
        {
            "fund_code": "510300",
            "trade_date": "2026-09-02",
            "open": "4.20",
            "high": "4.30",
            "low": "4.10",
            "close": "4.25",
            "volume": "120000",
            "amount": "510000",
        },
        {
            "fund_code": "510300",
            "trade_date": "2026-09-01",
            "open": "4.10",
            "high": "4.20",
            "low": "4.00",
            "close": "4.15",
            "volume": "110000",
            "amount": "450000",
        },
    ]


def test_adata_etf_parses_filters_sorts_and_deduplicates_rows() -> None:
    calls: list[dict[str, object]] = []
    rows = _adata_rows()
    rows.extend(
        [
            {**rows[0]},
            {**rows[0], "trade_date": "2026-09-03", "close": "9.99"},
            {**rows[0], "trade_date": "2026-09-01", "close": "--"},
            {**rows[0], "trade_date": "2026-09-01", "amount": "not-a-number"},
        ]
    )

    def fetcher(**kwargs: object) -> list[dict[str, object]]:
        calls.append(kwargs)
        return rows

    provider = ADataEtfHistoryProvider(
        frame_fetcher=fetcher,
        now=lambda: datetime(2026, 9, 3, 16, 0, tzinfo=_SHANGHAI_TZ),
    )
    result = provider.fetch_daily_bars(
        "510300",
        asset_type="etf",
        as_of=datetime(2026, 9, 3, 13, 0, tzinfo=_SHANGHAI_TZ),
        start=date(2026, 9, 1),
        end=date(2026, 9, 3),
    )

    assert [bar.date for bar in result.bars] == [date(2026, 9, 1), date(2026, 9, 2)]
    assert result.bars[0].volume == 110000
    assert result.bars[0].amount == 450000
    assert result.volume_unit == "shares"
    assert result.amount_unit == "CNY"
    assert result.raw_bar_count == 6
    assert result.dropped_bar_count == 4
    assert calls[0]["fund_code"] == "510300"
    assert calls[0]["k_type"] == 1
    assert calls[0]["adjustment"] == "qfq"
    assert calls[0]["path"] == "01"


@pytest.mark.parametrize(
    ("adjustment", "path"),
    [("none", "00"), ("qfq", "01"), ("hfq", "02")],
)
def test_adata_maps_adjustment_to_ths_daily_path(adjustment: str, path: str) -> None:
    calls: list[dict[str, object]] = []

    def fetcher(**kwargs: object) -> list[dict[str, object]]:
        calls.append(kwargs)
        return _adata_rows()

    result = ADataEtfHistoryProvider(frame_fetcher=fetcher).fetch_daily_bars(
        "510300",
        asset_type="etf",
        as_of=date(2026, 9, 2),
        start=date(2026, 9, 1),
        end=date(2026, 9, 2),
        adjustment=adjustment,
    )
    assert result.adjustment == adjustment
    assert calls[0]["adjustment"] == adjustment
    assert calls[0]["path"] == path


def test_adata_rejects_stock_without_dependency_lookup_or_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []
    subprocess_calls: list[object] = []
    monkeypatch.setattr(
        "trading_agent.swing.history_providers.subprocess.run",
        lambda *args, **kwargs: subprocess_calls.append((args, kwargs)),
    )
    provider = ADataEtfHistoryProvider(frame_fetcher=lambda **kwargs: calls.append(kwargs))

    with pytest.raises(HistoryUnsupportedError):
        provider.fetch_daily_bars("600001", asset_type="stock", as_of=date(2026, 9, 2))
    assert calls == []
    assert subprocess_calls == []


def test_adata_missing_dependency_and_empty_result_expose_source_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing_payload = json.dumps({"ok": False, "reason_code": "missing_dependency"})
    monkeypatch.setattr(
        "trading_agent.swing.history_providers.subprocess.run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, 0, missing_payload, ""
        ),
    )
    with pytest.raises(ADataHistoryError) as missing:
        ADataEtfHistoryProvider().fetch_daily_bars(
            "510300", asset_type="etf", as_of=date(2026, 9, 2)
        )
    assert missing.value.source == "adata"
    assert missing.value.reason_code == "missing_dependency"

    with pytest.raises(ADataHistoryError) as empty:
        ADataEtfHistoryProvider(frame_fetcher=lambda **kwargs: []).fetch_daily_bars(
            "510300", asset_type="etf", as_of=date(2026, 9, 2)
        )
    assert empty.value.source == "adata"
    assert empty.value.reason_code == "empty_result"


def test_adata_conflicting_duplicate_is_rejected_with_source() -> None:
    rows = _adata_rows()
    rows.append({**rows[0], "close": "4.99"})
    with pytest.raises(ADataHistoryError) as error_info:
        ADataEtfHistoryProvider(frame_fetcher=lambda **kwargs: rows).fetch_daily_bars(
            "510300", asset_type="etf", as_of=date(2026, 9, 2)
        )
    assert error_info.value.source == "adata"
    assert error_info.value.reason_code == "invalid_result"


def test_adata_subprocess_payload_supports_none_and_hfq(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []
    payload = json.dumps({"ok": True, "rows": _adata_rows()})

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, payload, "vendor output")

    monkeypatch.setattr("trading_agent.swing.history_providers.subprocess.run", run)
    for adjustment in ("none", "hfq"):
        result = ADataEtfHistoryProvider().fetch_daily_bars(
            "510300",
            asset_type="etf",
            as_of=date(2026, 9, 2),
            start=date(2026, 9, 1),
            end=date(2026, 9, 2),
            adjustment=adjustment,
        )
        assert result.adjustment == adjustment
        command = calls[-1][0]
        assert "--adjustment" in command
        assert command[command.index("--adjustment") + 1] == adjustment
        assert command[command.index("--code") + 1] == "510300"
        assert command[command.index("--start") + 1] == "2026-09-01"
        assert command[command.index("--end") + 1] == "2026-09-02"
        assert result.bar_count == 2


def test_adata_worker_filters_ths_text_and_keeps_stdout_as_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ths_text = (
        'quotebridge_v6_line_hs_510300_00_last36000('
        '{"total":3,"data":"20260901,4.1,4.2,4.0,4.15,110000,450000;'
        '20260902,4.2,4.3,4.1,4.25,120000,510000;'
        '20260903,4.3,4.4,4.2,4.35,130000,560000"});'
    )

    class FakeMarket:
        def get_market_etf_ths(self, **kwargs: object) -> list[dict[str, object]]:
            print("vendor chatter")
            return _adata_rows()

        def _get_text(self, url: str, code: str) -> str:
            return ths_text

    monkeypatch.setattr(
        _adata_worker.importlib,
        "import_module",
        lambda _: type("ADataModule", (), {"ETFMarketThs": FakeMarket}),
    )
    status = _adata_worker.main(
        [
            "--code",
            "510300",
            "--start",
            "2026-09-01",
            "--end",
            "2026-09-02",
            "--adjustment",
            "qfq",
        ]
    )
    captured = capsys.readouterr()
    assert status == 0
    assert "vendor chatter" not in captured.out
    assert "vendor chatter" in captured.err
    payload = json.loads(captured.out)
    assert payload["ok"] is True
    assert [row["trade_date"] for row in payload["rows"]] == [
        "2026-09-01",
        "2026-09-02",
    ]


def test_adata_worker_maps_unadjusted_and_hfq_ths_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class FakeMarket:
        def _get_text(self, url: str, code: str) -> str:
            calls.append(url)
            return (
                'quotebridge_v6_line_hs_510300_00_last36000('
                '{"total":1,"data":"20260901,4.1,4.2,4.0,4.15,110000,450000"});'
            )

    monkeypatch.setattr(
        _adata_worker.importlib,
        "import_module",
        lambda _: type("ADataModule", (), {"ETFMarketThs": FakeMarket}),
    )
    for adjustment, path in (("none", "00"), ("hfq", "02")):
        rows = _adata_worker._fetch_rows(
            code="510300",
            start=date(2026, 9, 1),
            end=date(2026, 9, 2),
            adjustment=adjustment,
        )
        assert rows and rows[0]["trade_date"] == "2026-09-01"
        assert f"/hs_510300/{path}/last36000.js" in calls[-1]


def test_adata_timeout_defaults_to_20_and_rejects_values_over_30() -> None:
    assert ADataEtfHistoryProvider().timeout_seconds == 20.0
    assert ADataEtfHistoryProvider(timeout_seconds=30.0).timeout_seconds == 30.0
    with pytest.raises(ValueError, match="at most 30 seconds"):
        ADataEtfHistoryProvider(timeout_seconds=30.01)


def test_adata_subprocess_timeout_is_bounded_and_socket_default_is_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}
    real_run = subprocess.run

    def timeout_run(command: list[str], **kwargs: object) -> object:
        observed.update(kwargs)
        return real_run(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            **kwargs,
        )

    monkeypatch.setattr("trading_agent.swing.history_providers.subprocess.run", timeout_run)
    original_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(7.5)
    started = time.monotonic()
    try:
        with pytest.raises(ADataHistoryError) as error_info:
            ADataEtfHistoryProvider(timeout_seconds=0.25).fetch_daily_bars(
                "510300", asset_type="etf", as_of=date(2026, 9, 2)
            )
        assert error_info.value.reason_code == "timeout"
        assert socket.getdefaulttimeout() == 7.5
    finally:
        socket.setdefaulttimeout(original_timeout)
    assert time.monotonic() - started < 3

    assert observed["shell"] is False
    assert observed["capture_output"] is True
    assert observed["text"] is True
    assert observed["timeout"] == 0.25
    assert observed["check"] is False


def test_adata_failure_allows_failover_to_next_provider() -> None:
    fallback_history = replace(
        _history(source="fallback"),
        code="510300",
        asset_type="etf",
        exchange="sh",
        secid="1.510300",
    )
    fallback = _FakeProvider("fallback", fallback_history)
    provider = FailoverHistoryProvider(
        [ADataEtfHistoryProvider(frame_fetcher=lambda **kwargs: []), fallback]
    )

    result = provider.fetch_daily_bars(
        "510300", asset_type="etf", as_of=date(2026, 9, 2)
    )

    assert result.source == "fallback"
    assert result.source_attempts[0].as_dict() == {
        "source": "adata",
        "status": "failed",
        "reason_code": "empty_result",
    }
    assert result.source_attempts[1].as_dict() == {
        "source": "fallback",
        "status": "success",
        "reason_code": "ok",
    }


class _BaoResponse:
    def __init__(
        self,
        *,
        code: str = "0",
        rows: object = None,
        row_code: str = "sh.600001",
        adjustflag: str = "2",
        iterable: bool = True,
    ) -> None:
        self.error_code = code
        self.error_msg = "private raw vendor detail"
        self.fields = [
            "date",
            "code",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "amount",
            "adjustflag",
        ]
        base_rows = _rows() if rows is None else rows
        self._rows = [
            {**row, "code": row_code, "adjustflag": adjustflag}
            for row in base_rows
        ]
        self._index = -1
        self._iterable = iterable

    def next(self) -> bool:
        if not self._iterable:
            raise AttributeError("legacy response has no iterator")
        self._index += 1
        return self._index < len(self._rows)

    def get_row_data(self) -> object:
        return self._rows[self._index].copy()

    def get_data(self) -> object:
        return self._rows


class _BaoModule:
    def __init__(
        self,
        *,
        query_code: str = "0",
        row_code: str = "sh.600001",
        adjustflag: str = "2",
        iterable: bool = True,
    ) -> None:
        self.calls: list[object] = []
        self.query_code = query_code
        self.row_code = row_code
        self.adjustflag = adjustflag
        self.iterable = iterable

    def login(self) -> _BaoResponse:
        self.calls.append("login")
        return _BaoResponse()

    def query_history_k_data_plus(self, *args: object, **kwargs: object) -> _BaoResponse:
        self.calls.append((args, kwargs))
        return _BaoResponse(
            code=self.query_code,
            row_code=self.row_code,
            adjustflag=self.adjustflag,
            iterable=self.iterable,
        )

    def logout(self) -> None:
        self.calls.append("logout")


def test_baostock_iteration_rejects_error_after_partial_page() -> None:
    class FailingPagedResponse(_BaoResponse):
        def next(self) -> bool:
            self._index += 1
            if self._index == 1:
                self.error_code = "1"
            return self._index < len(self._rows)

    class FailingPagedModule(_BaoModule):
        def query_history_k_data_plus(self, *args: object, **kwargs: object) -> _BaoResponse:
            self.calls.append((args, kwargs))
            return FailingPagedResponse()

    module = FailingPagedModule()
    with pytest.raises(HistoryDataError):
        BaoStockHistoryProvider(module).fetch_daily_bars(
            "600001", asset_type="stock", as_of=date(2026, 9, 1), adjustment="qfq"
        )
    assert module.calls[-1] == "logout"


def test_baostock_uses_actual_response_field_order() -> None:
    class ReorderedResponse(_BaoResponse):
        def __init__(self) -> None:
            super().__init__()
            self.fields = [
                "date",
                "code",
                "close",
                "open",
                "amount",
                "high",
                "adjustflag",
                "low",
                "volume",
            ]
            self._rows = [
                [
                    "2026-08-31",
                    "sh.600001",
                    "10.2",
                    "10.0",
                    "20000",
                    "10.5",
                    "2",
                    "9.8",
                    "1000",
                ]
            ]

    class ReorderedModule(_BaoModule):
        def query_history_k_data_plus(self, *args: object, **kwargs: object) -> _BaoResponse:
            self.calls.append((args, kwargs))
            return ReorderedResponse()

    module = ReorderedModule()
    result = BaoStockHistoryProvider(module).fetch_daily_bars(
        "600001", asset_type="stock", as_of=date(2026, 9, 1), adjustment="qfq"
    )
    assert result.bars[0].open == 10.0
    assert result.bars[0].close == 10.2
    assert result.bars[0].volume == 1000


def test_baostock_maps_code_adjustflag_and_always_logs_out() -> None:
    module = _BaoModule()
    provider = BaoStockHistoryProvider(
        module,
        now=lambda: datetime(2026, 9, 2, 16, 0, tzinfo=_SHANGHAI_TZ),
    )
    result = provider.fetch_daily_bars(
        "600001", asset_type="stock", as_of=date(2026, 9, 1), adjustment="qfq"
    )
    query_args, query_kwargs = module.calls[1]  # type: ignore[misc]
    assert query_args[0] == "sh.600001"
    assert query_args[1] == "date,code,open,high,low,close,volume,amount,adjustflag"
    assert query_kwargs["adjustflag"] == "2"
    assert result.volume_unit == "shares"
    assert result.amount_unit == "CNY"
    assert result.bar_count == 2
    assert module.calls[-1] == "logout"

    bad_module = _BaoModule(query_code="1")
    with pytest.raises(HistoryDataError):
        BaoStockHistoryProvider(bad_module).fetch_daily_bars(
            "600001", asset_type="stock", as_of=date(2026, 9, 1)
        )
    assert bad_module.calls[-1] == "logout"
    wrong_code_module = _BaoModule(row_code="sz.600001")
    with pytest.raises(HistoryDataError):
        BaoStockHistoryProvider(wrong_code_module).fetch_daily_bars(
            "600001", asset_type="stock", as_of=date(2026, 9, 1)
        )
    assert wrong_code_module.calls[-1] == "logout"
    wrong_flag_module = _BaoModule(adjustflag="1")
    with pytest.raises(HistoryDataError):
        BaoStockHistoryProvider(wrong_flag_module).fetch_daily_bars(
            "600001", asset_type="stock", as_of=date(2026, 9, 1)
        )
    assert wrong_flag_module.calls[-1] == "logout"
    legacy_module = _BaoModule(iterable=False)
    legacy_result = BaoStockHistoryProvider(legacy_module).fetch_daily_bars(
        "600001", asset_type="stock", as_of=date(2026, 9, 1)
    )
    assert legacy_result.bar_count == 2
    assert legacy_module.calls[-1] == "logout"
    with pytest.raises(ValueError):
        provider.fetch_daily_bars("not-a-code", asset_type="stock", as_of=date(2026, 9, 1))


def test_baostock_timeout_is_scoped_and_restored_on_success_and_exception() -> None:
    original_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(7.5)
    try:
        observed: list[float | None] = []

        class TimeoutResponse(_BaoResponse):
            def next(self) -> bool:
                observed.append(socket.getdefaulttimeout())
                return super().next()

            def get_row_data(self) -> object:
                observed.append(socket.getdefaulttimeout())
                return super().get_row_data()

        class TimeoutModule(_BaoModule):
            def login(self) -> _BaoResponse:
                observed.append(socket.getdefaulttimeout())
                return super().login()

            def query_history_k_data_plus(
                self, *args: object, **kwargs: object
            ) -> _BaoResponse:
                observed.append(socket.getdefaulttimeout())
                self.calls.append((args, kwargs))
                return TimeoutResponse()

            def logout(self) -> None:
                observed.append(socket.getdefaulttimeout())
                super().logout()

        successful_module = TimeoutModule()
        result = BaoStockHistoryProvider(successful_module, timeout_seconds=2.5).fetch_daily_bars(
            "600001", asset_type="stock", as_of=date(2026, 9, 1)
        )
        assert result.bar_count == 2
        assert observed and all(value == 2.5 for value in observed)
        assert socket.getdefaulttimeout() == 7.5

        observed.clear()

        class ErrorModule(TimeoutModule):
            def login(self) -> _BaoResponse:
                observed.append(socket.getdefaulttimeout())
                raise TimeoutError("vendor secret timeout detail")

        fallback = _FakeProvider("fallback", _history(source="fallback"))
        recovered = FailoverHistoryProvider(
            [BaoStockHistoryProvider(ErrorModule(), timeout_seconds=1.25), fallback]
        ).fetch_daily_bars("600001", asset_type="stock", as_of=date(2026, 9, 1))
        assert recovered.source == "fallback"
        assert recovered.source_attempts[0].reason_code == "provider_error"
        assert "vendor secret" not in repr(recovered.source_attempts)
        assert observed and all(value == 1.25 for value in observed)
        assert socket.getdefaulttimeout() == 7.5

        with pytest.raises(ValueError):
            BaoStockHistoryProvider(timeout_seconds=0)
    finally:
        socket.setdefaulttimeout(original_timeout)


def test_default_provider_order_has_future_additional_slot() -> None:
    additional = _FakeProvider("tushare", RuntimeError("disabled"))
    provider = build_default_history_provider((additional,))
    assert [type(item).__name__ for item in provider.providers] == [
        "EastmoneyHistoryProvider",
        "_FakeProvider",
        "ADataEtfHistoryProvider",
        "TencentHistoryProvider",
        "BaoStockHistoryProvider",
    ]


def test_missing_optional_dependencies_have_sanitized_reason_code(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing_dependency(_: str) -> object:
        raise ModuleNotFoundError("private package path")

    monkeypatch.setattr(
        "trading_agent.swing.history_providers.importlib.import_module", missing_dependency
    )
    for provider in (TencentHistoryProvider(), BaoStockHistoryProvider()):
        fallback = _FakeProvider("fallback", _history(source="fallback"))
        result = FailoverHistoryProvider([provider, fallback]).fetch_daily_bars(
            "600001", asset_type="stock", as_of=date(2026, 9, 1)
        )
        assert result.source == "fallback"
        assert result.source_attempts[0].reason_code == "missing_dependency"


@pytest.mark.parametrize(
    "untrusted_reason",
    ["token_secret", "http://private", "url_with_secret", "traceback_detail", "vendor_detail"],
)
def test_non_whitelist_reason_codes_never_enter_source_attempts(untrusted_reason: str) -> None:
    error = HistoryDataError("raw vendor detail")
    error.reason_code = untrusted_reason  # type: ignore[attr-defined]
    primary = _FakeProvider("primary", error)
    fallback = _FakeProvider("fallback", _history(source="fallback"))

    result = FailoverHistoryProvider([primary, fallback]).fetch_daily_bars(
        "600001", asset_type="stock", as_of=date(2026, 9, 1)
    )

    assert result.source_attempts[0].reason_code == "invalid_result"
    assert untrusted_reason not in repr(result.source_attempts)
