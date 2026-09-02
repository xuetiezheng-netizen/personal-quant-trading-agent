from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from trading_agent.swing.data import HistoryData, HistoryDataError
from trading_agent.swing.tushare_history import TushareHistoryProvider

SHANGHAI = timezone(timedelta(hours=8))


def _daily(day: str, *, open_: float = 10.0, close: float = 10.2, vol: float = 12.0, amount: float = 3.0) -> dict[str, object]:
    return {
        "trade_date": day,
        "open": open_,
        "high": max(open_, close) + 0.2,
        "low": min(open_, close) - 0.2,
        "close": close,
        "vol": vol,
        "amount": amount,
    }


def _factor(day: str, factor: float) -> dict[str, object]:
    return {"trade_date": day, "adj_factor": factor}


class FakePro:
    def __init__(self, daily: list[dict[str, object]], factors: list[dict[str, object]]) -> None:
        self.daily_rows = daily
        self.factor_rows = factors
        self.calls: list[tuple[str, dict[str, str]]] = []

    def daily(self, **kwargs: str) -> list[dict[str, object]]:
        self.calls.append(("daily", kwargs))
        return self.daily_rows

    def adj_factor(self, **kwargs: str) -> list[dict[str, object]]:
        self.calls.append(("adj_factor", kwargs))
        return self.factor_rows

    def fund_daily(self, **kwargs: str) -> list[dict[str, object]]:
        self.calls.append(("fund_daily", kwargs))
        return self.daily_rows

    def fund_adj(self, **kwargs: str) -> list[dict[str, object]]:
        self.calls.append(("fund_adj", kwargs))
        return self.factor_rows


class BrokenFactorPro(FakePro):
    def adj_factor(self, **_: str) -> object:
        raise AssertionError("none mode must not request adj_factor")

    def fund_adj(self, **_: str) -> object:
        raise AssertionError("none mode must not request fund_adj")


def _provider(
    pro: FakePro,
    *,
    now: datetime = datetime(2026, 9, 3, 16, tzinfo=SHANGHAI),
) -> TushareHistoryProvider:
    return TushareHistoryProvider(token="secret-token", pro_client=pro, now=lambda: now)


def test_token_is_required_and_never_appears_in_public_strings() -> None:
    with pytest.raises(ValueError, match="token is required") as missing:
        TushareHistoryProvider()
    assert "secret-token" not in str(missing.value)

    pro = FakePro([_daily("20260901")], [_factor("20260901", 2.0)])
    provider = _provider(pro)
    result = provider.fetch_daily_bars(
        "600001", asset_type="stock", start=date(2026, 9, 1), end=date(2026, 9, 1), adjustment="none"
    )
    public = " ".join((repr(provider), repr(result), result.source_url, repr(result.attempts)))
    assert "secret-token" not in public
    assert isinstance(result, HistoryData)


@pytest.mark.parametrize("adjustment, expected_open, expected_close", [
    ("none", 10.0, 10.2),
    ("qfq", 5.0, 5.1),
    ("hfq", 10.0, 10.2),
])
def test_stock_adjustment_formulas_and_unit_conversions(
    adjustment: str, expected_open: float, expected_close: float
) -> None:
    pro = FakePro(
        [_daily("20260901", vol=12.0, amount=3.0), _daily("20260902", open_=20.0, close=20.4)],
        [_factor("20260901", 1.0), _factor("20260902", 2.0)],
    )
    result = _provider(pro).fetch_daily_bars(
        "600001", asset_type="stock", start=date(2026, 9, 1), end=date(2026, 9, 2), adjustment=adjustment
    )

    assert result.source == "tushare"
    assert result.adjustment == adjustment
    assert result.complete is True
    assert result.bars[0].open == pytest.approx(expected_open)
    assert result.bars[0].close == pytest.approx(expected_close)
    assert result.bars[0].volume == pytest.approx(1200.0)
    assert result.bars[0].amount == pytest.approx(3000.0)
    assert result.volume_unit == "shares"
    assert result.amount_unit == "CNY"


def test_etf_uses_fund_endpoints_and_converts_code() -> None:
    pro = FakePro([_daily("20260901")], [_factor("20260901", 1.5)])
    result = _provider(pro).fetch_daily_bars(
        "510300", asset_type="etf", start=date(2026, 9, 1), end=date(2026, 9, 1), adjustment="hfq"
    )

    assert [name for name, _ in pro.calls] == ["fund_daily", "fund_adj"]
    assert all(call["ts_code"] == "510300.SH" for _, call in pro.calls)
    assert result.secid == "510300.SH"
    assert result.bars[0].close == pytest.approx(15.3)


def test_requests_use_expected_code_and_dates() -> None:
    pro = FakePro([_daily("20260901")], [_factor("20260901", 1.0)])
    _provider(pro).fetch_daily_bars(
        "000001", asset_type="stock", start=date(2026, 8, 31), end=date(2026, 9, 2), adjustment="qfq"
    )

    assert [name for name, _ in pro.calls] == ["daily", "adj_factor"]
    assert all(
        call == {
            "ts_code": "000001.SZ",
            "start_date": "20260831",
            "end_date": "20260902",
        }
        for _, call in pro.calls
    )


def test_none_mode_only_requests_daily_history() -> None:
    pro = BrokenFactorPro([_daily("20260901")], [])
    result = _provider(pro).fetch_daily_bars(
        "600001", asset_type="stock", start=date(2026, 9, 1), end=date(2026, 9, 1), adjustment="none"
    )

    assert result.adjustment == "none"
    assert [name for name, _ in pro.calls] == ["daily"]


@pytest.mark.parametrize("kind", ["daily", "adjustment"])
def test_returned_ts_code_must_match_request(kind: str) -> None:
    daily = _daily("20260901")
    factors = [_factor("20260901", 1.0)]
    if kind == "daily":
        daily["ts_code"] = "600002.SH"
    else:
        factors[0]["ts_code"] = "600002.SH"
    pro = FakePro([daily], factors)

    with pytest.raises(HistoryDataError, match="mismatched ts_code"):
        _provider(pro).fetch_daily_bars(
            "600001", asset_type="stock", start=date(2026, 9, 1), end=date(2026, 9, 1), adjustment="qfq"
        )


def test_intraday_and_future_rows_are_excluded() -> None:
    pro = FakePro(
        [_daily("20260901"), _daily("20260902", close=99.0), _daily("20260903", close=100.0)],
        [_factor("20260901", 1.0), _factor("20260902", 1.0), _factor("20260903", 1.0)],
    )
    result = _provider(pro, now=datetime(2026, 9, 2, 13, 0, tzinfo=SHANGHAI)).fetch_daily_bars(
        "600001", asset_type="stock", as_of=datetime(2026, 9, 2, 13, 0, tzinfo=SHANGHAI), adjustment="none"
    )

    assert [bar.date for bar in result.bars] == [date(2026, 9, 1)]
    assert result.dropped_bar_count == 2


@pytest.mark.parametrize(
    "factors, message",
    [
        ([_factor("20260901", 1.0)], "adjustment dates do not match"),
        ([_factor("20260901", 1.0), _factor("20260901", 1.0), _factor("20260902", 2.0)], "Duplicate"),
        ([_factor("20260901", 0.0), _factor("20260902", 2.0)], "must be positive"),
    ],
)
def test_factor_integrity_is_required(factors: list[dict[str, object]], message: str) -> None:
    pro = FakePro([_daily("20260901"), _daily("20260902")], factors)
    with pytest.raises(HistoryDataError, match=message):
        _provider(pro).fetch_daily_bars(
            "600001", asset_type="stock", start=date(2026, 9, 1), end=date(2026, 9, 2), adjustment="qfq"
        )


def test_client_failure_is_sanitized() -> None:
    token = "very-secret-token"

    class BrokenPro:
        def daily(self, **_: str) -> object:
            raise RuntimeError(f"upstream leaked {token}")

    provider = TushareHistoryProvider(
        token=token,
        pro=BrokenPro(),
        now=lambda: datetime(2026, 9, 3, 16, tzinfo=SHANGHAI),
    )
    with pytest.raises(HistoryDataError) as caught:
        provider.fetch_daily_bars(
            "600001", asset_type="stock", start=date(2026, 9, 1), end=date(2026, 9, 1), adjustment="none"
        )
    assert token not in str(caught.value)
    assert token not in repr(caught.value)
