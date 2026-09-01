from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest

from trading_agent.swing.data import (
    EastmoneyHistoryProvider,
    HistoryDataError,
    HistoryFetchError,
    infer_exchange,
)

_SHANGHAI_TZ = timezone(timedelta(hours=8))


def _row(day: str, *, open_: float = 10.0, close: float = 10.2, high: float = 10.4, low: float = 9.8) -> str:
    return f"{day},{open_},{close},{high},{low},1000,20000,1,2,0.2,0"


def _provider(rows: list[str], *, limit: int = 1300, max_retries: int = 3, seen: list[str] | None = None) -> EastmoneyHistoryProvider:
    def fetch_json(url: str) -> dict[str, object]:
        if seen is not None:
            seen.append(url)
        return {"data": {"klines": rows}}

    return EastmoneyHistoryProvider(
        limit=limit,
        max_retries=max_retries,
        retry_delay_seconds=0,
        fetch_json=fetch_json,
        sleep=lambda _: None,
        now=lambda: datetime(2026, 9, 1, 16, 0, tzinfo=_SHANGHAI_TZ),
    )


def test_intraday_and_future_bars_are_excluded_and_future_append_is_inert() -> None:
    rows = [_row("2026-08-31"), _row("2026-09-01"), _row("2026-09-02")]
    as_of = datetime(2026, 9, 1, 13, 22, tzinfo=_SHANGHAI_TZ)

    result = _provider(rows).fetch_daily_bars("600001", asset_type="stock", as_of=as_of)
    changed = _provider(rows + [_row("2026-09-03", close=99.0)]).fetch_daily_bars(
        "600001", asset_type="stock", as_of=as_of
    )

    assert [bar.date for bar in result.bars] == [date(2026, 8, 31)]
    assert result.bars == changed.bars
    assert result.completed_through == date(2026, 8, 31)
    assert result.dropped_bar_count == 2


def test_date_as_of_is_inclusive_but_after_close_datetime_is_also_inclusive() -> None:
    rows = [_row("2026-08-31"), _row("2026-09-01")]

    by_date = _provider(rows).fetch_daily_bars(
        "159999", asset_type="etf", as_of=date(2026, 9, 1)
    )
    after_close = _provider(rows).fetch_daily_bars(
        "159999", asset_type="etf", as_of=datetime(2026, 9, 1, 15, 0, tzinfo=_SHANGHAI_TZ)
    )

    assert [bar.date for bar in by_date.bars] == [date(2026, 8, 31), date(2026, 9, 1)]
    assert after_close.bars == by_date.bars


def test_rows_are_sorted_and_exact_duplicates_are_removed() -> None:
    rows = [_row("2026-09-02"), _row("2026-08-31"), _row("2026-09-01"), _row("2026-08-31")]

    result = _provider(rows).fetch_daily_bars(
        "600001", asset_type="stock", as_of=date(2026, 9, 2)
    )

    assert [bar.date for bar in result.bars] == [
        date(2026, 8, 31),
        date(2026, 9, 1),
        date(2026, 9, 2),
    ]
    assert result.raw_bar_count == 4
    assert result.bar_count == 3


def test_conflicting_duplicate_is_rejected() -> None:
    rows = [_row("2026-08-31"), _row("2026-08-31", close=10.9, high=11.1)]

    with pytest.raises(HistoryDataError, match="Conflicting duplicate"):
        _provider(rows).fetch_daily_bars("600001", asset_type="stock", as_of=date(2026, 9, 1))


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"high": 9.0}, "Inconsistent OHLC"),
        ({"low": 11.0}, "Inconsistent OHLC"),
        ({"open_": -1.0}, "OHLC must be positive"),
    ],
)
def test_bad_ohlc_is_rejected(kwargs: dict[str, float], message: str) -> None:
    with pytest.raises(HistoryDataError, match=message):
        _provider([_row("2026-08-31", **kwargs)]).fetch_daily_bars(
            "600001", asset_type="stock", as_of=date(2026, 9, 1)
        )


def test_negative_volume_or_amount_is_rejected() -> None:
    with pytest.raises(HistoryDataError, match="non-negative"):
        _provider(["2026-08-31,10,10.2,10.4,9.8,-1,20000"]).fetch_daily_bars(
            "600001", asset_type="stock", as_of=date(2026, 9, 1)
        )


def test_metadata_and_query_declare_source_adjustment_and_secid() -> None:
    seen: list[str] = []
    result = _provider([_row("2026-08-31")], limit=1300, seen=seen).fetch_daily_bars(
        "159999", asset_type="etf", as_of=date(2026, 9, 1)
    )

    query = parse_qs(urlparse(seen[0]).query)
    assert result.source == "eastmoney"
    assert result.adjustment == "qfq"
    assert result.secid == "0.159999"
    assert result.exchange == "sz"
    assert result.complete is True
    assert result.completed_through == date(2026, 8, 31)
    assert query["secid"] == ["0.159999"]
    assert query["fqt"] == ["1"]
    assert query["lmt"] == ["1300"]


def test_exchange_inference_is_explicit_and_unknown_code_requires_exchange() -> None:
    assert infer_exchange("600001", asset_type="stock") == "sh"
    assert infer_exchange("159999", asset_type="etf") == "sz"
    assert infer_exchange("600001", asset_type="etf", exchange="sz") == "sz"

    with pytest.raises(ValueError, match="Cannot infer"):
        infer_exchange("123456", asset_type="stock")


def test_network_failure_is_bounded_and_reported() -> None:
    calls = 0

    def fetch_json(_: str) -> dict[str, object]:
        nonlocal calls
        calls += 1
        raise OSError("simulated network outage")

    provider = EastmoneyHistoryProvider(
        max_retries=3,
        retry_delay_seconds=0,
        fetch_json=fetch_json,
        sleep=lambda _: None,
    )

    with pytest.raises(HistoryFetchError, match="bounded retries"):
        provider.fetch_daily_bars("600001", asset_type="stock", as_of=date(2026, 9, 1))
    assert calls == 3
