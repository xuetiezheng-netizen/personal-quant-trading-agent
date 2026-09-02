"""Optional Tushare Pro daily history for a single stock or ETF.

The provider is deliberately opt-in: callers must pass a non-empty token.  The
token is only retained long enough to initialise the lazily-created Tushare Pro
client and is never part of returned metadata or public error text.  Tests and
offline callers can inject a ``pro`` client, so importing this module never
requires the optional ``tushare`` package or a network connection.
"""

from __future__ import annotations

import importlib
import math
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from datetime import time as clock_time
from typing import Any, Literal, cast

from trading_agent.swing.data import (
    AssetType,
    Exchange,
    HistoryBar,
    HistoryData,
    HistoryDataError,
    infer_exchange,
)

Adjustment = Literal["none", "qfq", "hfq"]
Clock = Callable[[], datetime]

_SHANGHAI_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
_MARKET_CLOSE = clock_time(15, 0)
_SOURCE_URL = "https://tushare.pro"


class TushareHistoryError(HistoryDataError):
    """A Tushare history response could not be used safely."""


class TushareHistoryProvider:
    """Fetch and validate Tushare Pro daily history for one security.

    ``tushare`` is imported only when an injected client is not supplied and a
    fetch is actually attempted.  The public ``repr`` intentionally contains
    no configuration values, especially not the authentication token.
    """

    name = "tushare"

    def __init__(
        self,
        token: str | None = None,
        *,
        pro: Any | None = None,
        pro_client: Any | None = None,
        limit: int = 1300,
        now: Clock | None = None,
    ) -> None:
        if not isinstance(token, str) or not token.strip():
            raise ValueError("Tushare token is required")
        if pro is not None and pro_client is not None:
            raise ValueError("Provide only one Tushare Pro client")
        if limit <= 0:
            raise ValueError("limit must be positive")
        if limit > 5000:
            raise ValueError("limit cannot exceed 5000 public bars")

        self._token = token.strip()
        self._pro = pro if pro is not None else pro_client
        self._limit = limit
        self._now = now or (lambda: datetime.now(_SHANGHAI_TZ))

    def __repr__(self) -> str:
        """Return a safe representation that never includes the token."""

        return f"{type(self).__name__}(name={self.name!r}, enabled=True)"

    def fetch_daily_bars(
        self,
        code: str,
        *,
        asset_type: AssetType,
        as_of: date | datetime | None = None,
        start: date | datetime | None = None,
        end: date | datetime | None = None,
        adjustment: str = "qfq",
        exchange: Exchange | None = None,
    ) -> HistoryData:
        """Fetch one complete, date-bounded history using Tushare Pro.

        Tushare's unadjusted daily endpoints report volume in hands and amount
        in thousand CNY.  The returned ``HistoryBar`` always exposes shares and
        CNY after conversion.  Adjustment factors are required and fetched for
        ``qfq``/``hfq``; ``none`` intentionally uses only the daily endpoint so
        callers without factor permission can still request raw history.
        """

        normalized_code = _normalize_code(code)
        normalized_asset = _normalize_asset_type(asset_type)
        normalized_adjustment = _normalize_adjustment(adjustment)
        normalized_exchange = infer_exchange(
            normalized_code, asset_type=normalized_asset, exchange=exchange
        )
        ts_code = f"{normalized_code}.{normalized_exchange.upper()}"

        requested_as_of = as_of if as_of is not None else self._now()
        requested_end = _as_date(end) if end is not None else _as_date(requested_as_of)
        requested_start = (
            _as_date(start)
            if start is not None
            else requested_end - timedelta(days=self._limit * 3)
        )
        if requested_start > requested_end:
            raise ValueError("start cannot be later than end")

        completion_cutoff = _completed_cutoff(requested_as_of)
        effective_cutoff = min(requested_end, completion_cutoff)
        params = {
            "ts_code": ts_code,
            "start_date": requested_start.strftime("%Y%m%d"),
            "end_date": requested_end.strftime("%Y%m%d"),
        }
        daily_method, factor_method = _method_names(normalized_asset)
        client = self._get_pro_client()
        daily_rows = _records(
            self._call(client, daily_method, params),
            kind="daily",
            code=normalized_code,
        )
        _validate_row_codes(daily_rows, ts_code=ts_code, code=normalized_code, kind="daily")

        raw_bars, dropped = _select_daily_bars(
            daily_rows,
            code=normalized_code,
            start=requested_start,
            cutoff=effective_cutoff,
        )
        if not raw_bars:
            raise TushareHistoryError(
                f"No completed daily bars for {normalized_code} through {effective_cutoff.isoformat()}"
            )

        if normalized_adjustment == "none":
            bars = tuple(
                _adjust_bar(
                    raw_bars[day],
                    factor=1.0,
                    latest_factor=1.0,
                    adjustment=normalized_adjustment,
                    code=normalized_code,
                )
                for day in sorted(raw_bars)
            )
        else:
            factor_rows = _records(
                self._call(client, factor_method, params),
                kind="adjustment",
                code=normalized_code,
            )
            _validate_row_codes(
                factor_rows,
                ts_code=ts_code,
                code=normalized_code,
                kind="adjustment",
            )
            factors = _select_factors(
                factor_rows,
                code=normalized_code,
                start=requested_start,
                cutoff=effective_cutoff,
            )
            daily_dates = set(raw_bars)
            factor_dates = set(factors)
            if daily_dates != factor_dates:
                raise TushareHistoryError(
                    f"Tushare adjustment dates do not match daily dates for {normalized_code}"
                )

            latest_factor = factors[max(factors)]
            bars = tuple(
                _adjust_bar(
                    raw_bars[day],
                    factor=factors[day],
                    latest_factor=latest_factor,
                    adjustment=normalized_adjustment,
                    code=normalized_code,
                )
                for day in sorted(raw_bars)
            )

        fetched_at = self._now()
        return HistoryData(
            code=normalized_code,
            asset_type=normalized_asset,
            exchange=normalized_exchange,
            secid=ts_code,
            bars=bars,
            source=self.name,
            source_url=_SOURCE_URL,
            adjustment=normalized_adjustment,
            requested_as_of=requested_as_of,
            requested_start=requested_start,
            requested_end=requested_end,
            completed_through=bars[-1].date,
            fetched_at=fetched_at,
            raw_bar_count=len(daily_rows),
            dropped_bar_count=dropped,
            complete=True,
            volume_unit="shares",
            amount_unit="CNY",
        )

    fetch_daily_history = fetch_daily_bars

    def _get_pro_client(self) -> Any:
        if self._pro is not None:
            return self._pro
        try:
            module = importlib.import_module("tushare")
            pro_api = module.pro_api
            client = pro_api(self._token)
        except Exception:  # noqa: BLE001 - sanitize all optional-client failures
            raise TushareHistoryError("Tushare client is unavailable") from None
        self._pro = client
        return client

    @staticmethod
    def _call(client: Any, method_name: str, params: Mapping[str, str]) -> object:
        try:
            method = getattr(client, method_name)
            return method(**params)
        except Exception:  # noqa: BLE001 - third-party errors must be sanitized
            # Do not chain the client exception: a third-party message may echo
            # credentials or request details.
            raise TushareHistoryError("Tushare history request failed") from None


def fetch_daily_bars(
    code: str,
    *,
    token: str | None = None,
    pro: Any | None = None,
    pro_client: Any | None = None,
    asset_type: AssetType,
    as_of: date | datetime | None = None,
    start: date | datetime | None = None,
    end: date | datetime | None = None,
    adjustment: str = "qfq",
    exchange: Exchange | None = None,
    limit: int = 1300,
    now: Clock | None = None,
) -> HistoryData:
    """Functional entry point; token remains explicit and opt-in."""

    provider = TushareHistoryProvider(
        token,
        pro=pro,
        pro_client=pro_client,
        limit=limit,
        now=now,
    )
    return provider.fetch_daily_bars(
        code,
        asset_type=asset_type,
        as_of=as_of,
        start=start,
        end=end,
        adjustment=adjustment,
        exchange=exchange,
    )


fetch_daily_history = fetch_daily_bars


def _method_names(asset_type: AssetType) -> tuple[str, str]:
    if asset_type == "stock":
        return "daily", "adj_factor"
    return "fund_daily", "fund_adj"


def _records(payload: object, *, kind: str, code: str) -> list[Mapping[str, Any]]:
    """Convert a Tushare DataFrame or test-friendly row sequence to records."""

    try:
        if hasattr(payload, "to_dict"):
            converter = payload.to_dict
            try:
                rows = converter(orient="records")
            except TypeError:
                rows = converter("records")
        elif isinstance(payload, Mapping):
            if "trade_date" not in payload:
                raise ValueError
            rows = [payload]
        elif isinstance(payload, Sequence) and not isinstance(payload, (str, bytes, bytearray)):
            rows = list(payload)
        else:
            raise ValueError
    except Exception:  # noqa: BLE001 - normalize all provider response shapes
        raise TushareHistoryError(f"Tushare {kind} response is invalid for {code}") from None

    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes, bytearray)):
        raise TushareHistoryError(f"Tushare {kind} response is invalid for {code}")
    if not all(isinstance(row, Mapping) for row in rows):
        raise TushareHistoryError(f"Tushare {kind} response has invalid rows for {code}")
    return [cast(Mapping[str, Any], row) for row in rows]


def _select_daily_bars(
    rows: Sequence[Mapping[str, Any]],
    *,
    code: str,
    start: date,
    cutoff: date,
) -> tuple[dict[date, HistoryBar], int]:
    selected: dict[date, HistoryBar] = {}
    dropped = 0
    for row in rows:
        day = _row_date(row, code, "daily")
        if day < start or day > cutoff:
            dropped += 1
            continue
        bar = _raw_bar(row, day, code)
        previous = selected.get(day)
        if previous is not None and previous != bar:
            raise TushareHistoryError(
                f"Conflicting duplicate daily bar for {code} on {day.isoformat()}"
            )
        selected[day] = bar
    return selected, dropped


def _select_factors(
    rows: Sequence[Mapping[str, Any]],
    *,
    code: str,
    start: date,
    cutoff: date,
) -> dict[date, float]:
    selected: dict[date, float] = {}
    for row in rows:
        day = _row_date(row, code, "adjustment")
        if day < start or day > cutoff:
            continue
        factor = _number(row, "adj_factor", code, kind="adjustment")
        if factor <= 0:
            raise TushareHistoryError(
                f"Tushare adjustment factor must be positive for {code} on {day.isoformat()}"
            )
        if day in selected:
            raise TushareHistoryError(
                f"Duplicate Tushare adjustment factor for {code} on {day.isoformat()}"
            )
        selected[day] = factor
    return selected


def _validate_row_codes(
    rows: Sequence[Mapping[str, Any]], *, ts_code: str, code: str, kind: str
) -> None:
    for row in rows:
        if "ts_code" not in row:
            continue
        value = row["ts_code"]
        if not isinstance(value, str) or value.strip().upper() != ts_code:
            raise TushareHistoryError(f"Tushare {kind} row has mismatched ts_code for {code}")


def _raw_bar(row: Mapping[str, Any], day: date, code: str) -> HistoryBar:
    open_price = _number(row, "open", code, kind="daily")
    high_price = _number(row, "high", code, kind="daily")
    low_price = _number(row, "low", code, kind="daily")
    close_price = _number(row, "close", code, kind="daily")
    volume_lots = _number(row, "vol", code, kind="daily")
    amount_thousand_cny = _number(row, "amount", code, kind="daily")
    if volume_lots < 0 or amount_thousand_cny < 0:
        raise TushareHistoryError(
            f"Volume and amount must be non-negative for {code} on {day.isoformat()}"
        )
    bar = HistoryBar(
        date=day,
        open=open_price,
        high=high_price,
        low=low_price,
        close=close_price,
        volume=volume_lots * 100.0,
        amount=amount_thousand_cny * 1000.0,
    )
    _validate_bar(bar, code)
    return bar


def _adjust_bar(
    raw_bar: HistoryBar,
    *,
    factor: float,
    latest_factor: float,
    adjustment: Adjustment,
    code: str,
) -> HistoryBar:
    if adjustment == "none":
        multiplier = 1.0
    elif adjustment == "qfq":
        multiplier = factor / latest_factor
    else:
        multiplier = factor

    bar = HistoryBar(
        date=raw_bar.date,
        open=raw_bar.open * multiplier,
        high=raw_bar.high * multiplier,
        low=raw_bar.low * multiplier,
        close=raw_bar.close * multiplier,
        volume=raw_bar.volume,
        amount=raw_bar.amount,
    )
    _validate_bar(bar, code)
    return bar


def _validate_bar(bar: HistoryBar, code: str) -> None:
    values = (bar.open, bar.high, bar.low, bar.close, bar.volume, bar.amount)
    if not all(math.isfinite(value) for value in values):
        raise TushareHistoryError(f"Non-finite daily-bar fields for {code} on {bar.date.isoformat()}")
    if min(bar.open, bar.high, bar.low, bar.close) <= 0:
        raise TushareHistoryError(f"OHLC must be positive for {code} on {bar.date.isoformat()}")
    if bar.volume < 0 or bar.amount < 0:
        raise TushareHistoryError(
            f"Volume and amount must be non-negative for {code} on {bar.date.isoformat()}"
        )
    if (
        bar.low > min(bar.open, bar.close)
        or bar.high < max(bar.open, bar.close)
        or bar.low > bar.high
    ):
        raise TushareHistoryError(f"Inconsistent OHLC for {code} on {bar.date.isoformat()}")


def _row_date(row: Mapping[str, Any], code: str, kind: str) -> date:
    if "trade_date" not in row:
        raise TushareHistoryError(f"Tushare {kind} row has no trade_date for {code}")
    return _parse_date(row["trade_date"], code=code, kind=kind)


def _parse_date(value: object, *, code: str, kind: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for candidate in (text, text.replace(" ", "T", 1)):
        try:
            return datetime.fromisoformat(candidate).date()
        except ValueError:
            pass
    if len(text) == 8 and text.isdigit():
        try:
            return date(int(text[:4]), int(text[4:6]), int(text[6:]))
        except ValueError:
            pass
    raise TushareHistoryError(f"Tushare {kind} row has invalid trade_date for {code}") from None


def _number(row: Mapping[str, Any], field: str, code: str, *, kind: str) -> float:
    if field not in row:
        raise TushareHistoryError(f"Tushare {kind} row has no {field} for {code}")
    try:
        value = float(row[field])
    except (TypeError, ValueError, OverflowError):
        raise TushareHistoryError(f"Tushare {kind} row has invalid {field} for {code}") from None
    if not math.isfinite(value):
        raise TushareHistoryError(f"Tushare {kind} row has invalid {field} for {code}")
    return value


def _normalize_code(code: str) -> str:
    normalized = str(code).strip()
    if len(normalized) != 6 or not normalized.isdigit():
        # Keep invalid caller input out of public errors; the same boundary is
        # used to ensure an accidentally reused credential can never be echoed.
        raise ValueError("Invalid A-share code")
    return normalized


def _normalize_asset_type(asset_type: AssetType) -> AssetType:
    normalized = str(asset_type).strip().lower()
    if normalized not in {"stock", "etf"}:
        raise ValueError("asset_type must be 'stock' or 'etf'")
    return cast(AssetType, normalized)


def _normalize_adjustment(adjustment: str) -> Adjustment:
    normalized = str(adjustment).strip().lower()
    if normalized not in {"none", "qfq", "hfq"}:
        raise ValueError("adjustment must be 'qfq', 'hfq' or 'none'")
    return cast(Adjustment, normalized)


def _as_date(value: date | datetime) -> date:
    return value.date() if isinstance(value, datetime) else value


def _completed_cutoff(value: date | datetime) -> date:
    if isinstance(value, datetime):
        local = value.astimezone(_SHANGHAI_TZ) if value.tzinfo is not None else value
        if local.timetz().replace(tzinfo=None) < _MARKET_CLOSE:
            return local.date() - timedelta(days=1)
        return local.date()
    return value


__all__ = [
    "TushareHistoryError",
    "TushareHistoryProvider",
    "fetch_daily_bars",
    "fetch_daily_history",
]
