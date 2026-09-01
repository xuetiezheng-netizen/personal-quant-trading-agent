"""面向持仓波段回测的东方财富日线数据适配器。

该模块和现有的全市场扫描 provider 分开，专门处理“给定一只沪深股票/ETF”的
日线历史。它只读东方财富公开接口，不保存原始响应，也不接触持仓文件。

数据使用东方财富 ``fqt=1`` 的前复权（qfq）日线。接口有时会在盘中返回当天尚未
收盘的 K 线，因此 ``as_of`` 为盘中时间时会主动排除当天；所有返回的 K 线还会在
进入结果前经过日期、OHLC 和数值完整性校验。
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from datetime import time as clock_time
from typing import Any, Literal
from urllib.parse import urlencode
from urllib.request import Request, urlopen

AssetType = Literal["stock", "etf"]
Exchange = Literal["sh", "sz"]
JsonObject = Mapping[str, Any]
JsonFetcher = Callable[[str], JsonObject]
Clock = Callable[[], datetime]
Sleep = Callable[[float], None]

_SHANGHAI_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
_MARKET_CLOSE = clock_time(15, 0)


class HistoryDataError(RuntimeError):
    """历史行情无法安全用于分析。"""


class HistoryFetchError(HistoryDataError):
    """公开行情接口在有限重试后仍不可用。"""


@dataclass(frozen=True, slots=True)
class HistoryBar:
    """一根已经通过完整性校验的日线 K 线。

    字段名刻意使用 ``date/open/high/low/close/volume/amount``，让上层策略不需要
    知道东方财富返回字段的 ``f51``--``f61`` 编号。volume 和 amount 保留数据源原
    生单位；当前东方财富日线的成交额字段单位为人民币元，成交量单位由数据源
    返回，不在此处擅自换算。
    """

    date: date
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float

    # 兼容已有 DailyBar 的读取习惯，同时保留上层要求的短字段名。
    @property
    def trade_date(self) -> date:
        return self.date

    @property
    def open_price(self) -> float:
        return self.open

    @property
    def high_price(self) -> float:
        return self.high

    @property
    def low_price(self) -> float:
        return self.low

    @property
    def close_price(self) -> float:
        return self.close

    @property
    def turnover_amount(self) -> float:
        return self.amount


@dataclass(frozen=True, slots=True)
class HistoryData:
    """单个标的的、按 ``as_of`` 截止的历史结果及其可追溯元数据。

    ``complete`` 表示返回行均已通过结构化完整性校验；它不声称数据源没有漏掉
    某个交易日。公开接口没有携带交易日历，所以完整性边界必须由调用方结合
    ``bar_count``、``requested_start`` 和 ``completed_through`` 进一步判断。
    """

    code: str
    asset_type: AssetType
    exchange: Exchange
    secid: str
    bars: tuple[HistoryBar, ...]
    source: str
    source_url: str
    adjustment: str
    requested_as_of: date | datetime
    requested_start: date
    requested_end: date
    completed_through: date
    fetched_at: datetime
    raw_bar_count: int
    dropped_bar_count: int
    complete: bool = True
    volume_unit: str = "source_native"
    amount_unit: str = "CNY"

    @property
    def data_as_of(self) -> date:
        """最后一根被纳入结果的完整日线日期。"""

        return self.completed_through

    @property
    def latest_date(self) -> date:
        return self.completed_through

    @property
    def earliest_date(self) -> date:
        return self.bars[0].date

    @property
    def bar_count(self) -> int:
        return len(self.bars)


class EastmoneyHistoryProvider:
    """抓取指定沪深股票/ETF的东方财富前复权日线历史。

    ``asset_type`` 是必填的，``exchange`` 可显式传入；缺省时只按明确的代码
    规则推断，绝不按名称猜测交易所。默认 ``limit=1300`` 适合中低频波段回测，
    但不等于保证一定有 1300 个交易日，结果中的 ``bar_count`` 才是实际数量。
    """

    name = "eastmoney"
    _HISTORY_ENDPOINT = "https://push2his.eastmoney.com/api/qt/stock/kline/get"

    def __init__(
        self,
        *,
        limit: int = 1300,
        max_retries: int = 3,
        retry_delay_seconds: float = 2.0,
        fetch_json: JsonFetcher | None = None,
        sleep: Sleep = time.sleep,
        now: Clock | None = None,
    ) -> None:
        if limit <= 0 or max_retries <= 0:
            raise ValueError("limit and max_retries must be positive")
        if limit > 5000:
            raise ValueError("limit cannot exceed 5000 public bars")
        if retry_delay_seconds < 0:
            raise ValueError("retry delay cannot be negative")
        self._limit = limit
        self._max_retries = max_retries
        self._retry_delay_seconds = retry_delay_seconds
        self._fetch_json = fetch_json or _urlopen_json
        self._sleep = sleep
        self._now = now or (lambda: datetime.now(_SHANGHAI_TZ))

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
        """获取并校验单个标的的日线历史。

        ``datetime`` 类型的 ``as_of`` 代表实际观察时刻：北京时间 15:00 前，
        当天 K 线一律视为未收盘并排除。单独传 ``date`` 时按该日期收盘后的回放
        语义处理，日期本身可以进入结果。
        """

        normalized_code = _normalize_code(code)
        normalized_asset_type = _normalize_asset_type(asset_type)
        normalized_adjustment = _normalize_adjustment(adjustment)
        normalized_exchange = infer_exchange(
            normalized_code, asset_type=normalized_asset_type, exchange=exchange
        )
        secid = f"{1 if normalized_exchange == 'sh' else 0}.{normalized_code}"

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
        url = self._history_url(
            secid=secid,
            start=requested_start,
            end=requested_end,
            adjustment=normalized_adjustment,
        )
        payload = self._fetch_with_retry(url)
        raw_rows = _extract_rows(payload, normalized_code)
        bars, dropped = _filter_and_validate_rows(
            raw_rows,
            code=normalized_code,
            start=requested_start,
            cutoff=effective_cutoff,
        )
        if not bars:
            raise HistoryDataError(
                f"No completed daily bars for {normalized_code} through {effective_cutoff.isoformat()}"
            )

        fetched_at = self._now()
        return HistoryData(
            code=normalized_code,
            asset_type=normalized_asset_type,
            exchange=normalized_exchange,
            secid=secid,
            bars=bars,
            source=self.name,
            source_url=url,
            adjustment=normalized_adjustment,
            requested_as_of=requested_as_of,
            requested_start=requested_start,
            requested_end=requested_end,
            completed_through=bars[-1].date,
            fetched_at=fetched_at,
            raw_bar_count=len(raw_rows),
            dropped_bar_count=dropped,
            complete=True,
        )

    # A descriptive alias is convenient for callers that name the operation after
    # the returned object rather than the individual bars.
    fetch_daily_history = fetch_daily_bars

    def _history_url(
        self,
        *,
        secid: str,
        start: date,
        end: date,
        adjustment: str,
    ) -> str:
        query = urlencode(
            {
                "secid": secid,
                "ut": "fa5fd1943c7b386f172d6893dbfba10b",
                "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
                "klt": 101,
                "fqt": _adjustment_code(adjustment),
                "beg": start.strftime("%Y%m%d"),
                "end": end.strftime("%Y%m%d"),
                "lmt": self._limit,
            }
        )
        return f"{self._HISTORY_ENDPOINT}?{query}"

    def _fetch_with_retry(self, url: str) -> JsonObject:
        last_error: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                payload = self._fetch_json(url)
                if not isinstance(payload, Mapping):
                    raise HistoryDataError("Public endpoint returned a non-object JSON payload")
                return payload
            except Exception as exc:  # noqa: BLE001 - provider errors are bounded below
                last_error = exc
                if attempt + 1 < self._max_retries:
                    self._sleep(self._retry_delay_seconds * (2**attempt))
        raise HistoryFetchError("Eastmoney history endpoint was unavailable after bounded retries") from last_error


def fetch_daily_bars(
    code: str,
    *,
    asset_type: AssetType,
    as_of: date | datetime | None = None,
    start: date | datetime | None = None,
    end: date | datetime | None = None,
    adjustment: str = "qfq",
    exchange: Exchange | None = None,
    limit: int = 1300,
    max_retries: int = 3,
    retry_delay_seconds: float = 2.0,
    fetch_json: JsonFetcher | None = None,
    sleep: Sleep = time.sleep,
    now: Clock | None = None,
) -> HistoryData:
    """函数式入口；适合脚本或上层服务直接调用。"""

    provider = EastmoneyHistoryProvider(
        limit=limit,
        max_retries=max_retries,
        retry_delay_seconds=retry_delay_seconds,
        fetch_json=fetch_json,
        sleep=sleep,
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


# 语义化别名，便于上层不用绑定具体类名。
fetch_daily_history = fetch_daily_bars
EastmoneyDailyHistoryProvider = EastmoneyHistoryProvider


def infer_exchange(
    code: str,
    *,
    asset_type: AssetType,
    exchange: Exchange | None = None,
) -> Exchange:
    """按显式代码规则推断沪/深，或校验调用方给出的交易所。"""

    normalized_code = _normalize_code(code)
    normalized_asset_type = _normalize_asset_type(asset_type)
    if exchange is not None:
        normalized_exchange = str(exchange).strip().lower()
        if normalized_exchange not in {"sh", "sz"}:
            raise ValueError("exchange must be 'sh' or 'sz'")
        return normalized_exchange  # type: ignore[return-value]

    if normalized_asset_type == "stock":
        if normalized_code.startswith(("000", "001", "002", "003", "300", "301")):
            return "sz"
        if normalized_code.startswith(("600", "601", "603", "605", "688", "689")):
            return "sh"
    else:  # ETF：沪市 ETF 以 5/56/58 开头，深市 ETF 以 15/16/18 开头。
        if normalized_code.startswith(("510", "511", "512", "513", "515", "516", "517", "518", "560", "561", "562", "588")):
            return "sh"
        if normalized_code.startswith(("150", "159", "160", "161", "162", "163", "164", "165", "166", "167", "168", "169", "180", "184")):
            return "sz"
    raise ValueError(
        f"Cannot infer Shanghai/Shenzhen exchange for {normalized_asset_type} code {normalized_code}; "
        "pass exchange explicitly"
    )


def security_id(code: str, *, asset_type: AssetType, exchange: Exchange | None = None) -> str:
    """返回东方财富所需的 ``secid``，不接收名称作为推断依据。"""

    normalized_code = _normalize_code(code)
    resolved_exchange = infer_exchange(
        normalized_code, asset_type=asset_type, exchange=exchange
    )
    return f"{1 if resolved_exchange == 'sh' else 0}.{normalized_code}"


def _security_id(code: str, *, asset_type: AssetType = "stock", exchange: Exchange | None = None) -> str:
    """兼容内部/旧调用方的私有命名。"""

    return security_id(code, asset_type=asset_type, exchange=exchange)


def _urlopen_json(url: str) -> JsonObject:
    request = Request(
        url,
        headers={
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://quote.eastmoney.com/",
            "User-Agent": "PersonalQuantTradingAgent/0.1 (+local-research)",
        },
    )
    with urlopen(request, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, Mapping):
        raise HistoryDataError("Public endpoint JSON root is not an object")
    return payload


def _extract_rows(payload: JsonObject, code: str) -> list[str]:
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise HistoryDataError(f"History response for {code} has no data object")
    rows = data.get("klines")
    if not isinstance(rows, list) or not all(isinstance(row, str) for row in rows):
        raise HistoryDataError(f"History response for {code} has no valid kline list")
    return list(rows)


def _filter_and_validate_rows(
    rows: Sequence[str],
    *,
    code: str,
    start: date,
    cutoff: date,
) -> tuple[tuple[HistoryBar, ...], int]:
    selected: dict[date, HistoryBar] = {}
    dropped = 0
    for raw in rows:
        raw_date = _raw_date(raw, code)
        if raw_date < start or raw_date > cutoff:
            dropped += 1
            continue
        bar = _parse_bar(raw, code)
        previous = selected.get(bar.date)
        if previous is not None and previous != bar:
            raise HistoryDataError(f"Conflicting duplicate daily bar for {code} on {bar.date}")
        selected[bar.date] = bar

    bars = tuple(selected[day] for day in sorted(selected))
    return bars, dropped


def _raw_date(raw: str, code: str) -> date:
    if not isinstance(raw, str):
        raise HistoryDataError(f"Invalid daily-bar record for {code}")
    values = raw.split(",")
    if len(values) < 7:
        raise HistoryDataError(f"Incomplete daily-bar record for {code}")
    try:
        return datetime.fromisoformat(values[0].strip()).date()
    except ValueError as exc:
        raise HistoryDataError(f"Invalid daily-bar date for {code}") from exc


def _parse_bar(raw: str, code: str) -> HistoryBar:
    values = raw.split(",")
    try:
        bar = HistoryBar(
            date=datetime.fromisoformat(values[0].strip()).date(),
            open=float(values[1]),
            close=float(values[2]),
            high=float(values[3]),
            low=float(values[4]),
            volume=float(values[5]),
            amount=float(values[6]),
        )
    except (IndexError, TypeError, ValueError) as exc:
        raise HistoryDataError(f"Invalid daily-bar fields for {code}") from exc
    _validate_bar(bar, code)
    return bar


def _validate_bar(bar: HistoryBar, code: str) -> None:
    values = (bar.open, bar.high, bar.low, bar.close, bar.volume, bar.amount)
    if not all(math.isfinite(value) for value in values):
        raise HistoryDataError(f"Non-finite daily-bar fields for {code} on {bar.date}")
    if min(bar.open, bar.high, bar.low, bar.close) <= 0:
        raise HistoryDataError(f"OHLC must be positive for {code} on {bar.date}")
    if bar.volume < 0 or bar.amount < 0:
        raise HistoryDataError(f"Volume and amount must be non-negative for {code} on {bar.date}")
    if bar.low > min(bar.open, bar.close) or bar.high < max(bar.open, bar.close) or bar.low > bar.high:
        raise HistoryDataError(f"Inconsistent OHLC for {code} on {bar.date}")


def _completed_cutoff(value: date | datetime) -> date:
    if isinstance(value, datetime):
        local = value.astimezone(_SHANGHAI_TZ) if value.tzinfo is not None else value
        if local.timetz().replace(tzinfo=None) < _MARKET_CLOSE:
            return local.date() - timedelta(days=1)
        return local.date()
    return value


def _as_date(value: date | datetime) -> date:
    return value.date() if isinstance(value, datetime) else value


def _normalize_code(code: str) -> str:
    normalized = str(code).strip()
    if len(normalized) != 6 or not normalized.isdigit():
        raise ValueError(f"Invalid A-share code: {code!r}")
    return normalized


def _normalize_asset_type(asset_type: AssetType) -> AssetType:
    normalized = str(asset_type).strip().lower()
    if normalized not in {"stock", "etf"}:
        raise ValueError("asset_type must be 'stock' or 'etf'")
    return normalized  # type: ignore[return-value]


def _normalize_adjustment(adjustment: str) -> str:
    normalized = str(adjustment).strip().lower()
    if normalized not in {"qfq", "hfq", "none"}:
        raise ValueError("adjustment must be 'qfq', 'hfq' or 'none'")
    return normalized


def _adjustment_code(adjustment: str) -> int:
    return {"none": 0, "qfq": 1, "hfq": 2}[adjustment]
