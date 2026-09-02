"""可插拔的单标的历史日线 provider 及其安全降级核心。

本模块只处理公开日线。每次请求最多接受一个 provider 的完整结果；不同来源
之间不会按日期拼接，失败原因也只以有限的公开原因码向上层暴露。
"""

from __future__ import annotations

import importlib
import json
import math
import re
import socket
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from itertools import pairwise
from pathlib import Path
from threading import RLock
from typing import ClassVar, Protocol, runtime_checkable

from trading_agent.swing.data import (
    AssetType,
    EastmoneyHistoryProvider,
    Exchange,
    HistoryBar,
    HistoryData,
    HistoryDataError,
    HistorySourceAttempt,
    _as_date,
    _completed_cutoff,
    _normalize_adjustment,
    _normalize_asset_type,
    _normalize_code,
    _validate_bar,
    infer_exchange,
    security_id,
)

_SHANGHAI_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
_DEFAULT_LIMIT = 1300
# BaoStock 0.9.x uses the process-global socket default timeout. This lock
# serializes cooperating calls in this module; it cannot isolate unrelated
# sockets or threads that do not use this lock.
_BAOSTOCK_LOCK = RLock()
_DEFAULT_BAOSTOCK_TIMEOUT_SECONDS = 20.0
_DEFAULT_ADATA_TIMEOUT_SECONDS = 20.0
_REASON_CODES = {
    "ok",
    "provider_error",
    "invalid_result",
    "unsupported_asset",
    "missing_dependency",
    "empty_result",
    "missing_field",
    "invalid_response",
    "request_error",
    "query_error",
    "timeout",
}


@runtime_checkable
class DailyHistoryProvider(Protocol):
    """统一的单标的历史日线 provider 协议。"""

    name: str

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
        ...


class HistoryUnsupportedError(HistoryDataError):
    """当前 provider 明确不支持本次请求。"""

    def __init__(self, reason_code: str = "unsupported_asset") -> None:
        self.public_reason_code = _safe_reason_code(reason_code, fallback="unsupported_asset")
        self.reason_code = self.public_reason_code
        # 不把调用方消息、代码、token 或底层异常放入公开异常文本。
        super().__init__(self.public_reason_code)


class HistoryFailoverError(HistoryDataError):
    """所有已配置 provider 都失败或返回了不合格结果。"""

    def __init__(self, attempts: Sequence[HistorySourceAttempt]) -> None:
        self.attempts = tuple(attempts)
        self.source_attempts = self.attempts
        super().__init__("No configured history provider returned a safe history result")


class _InvalidHistoryResult(HistoryDataError):
    """带内部原因码的结果校验失败；异常文本不会离开 failover 边界。"""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = _safe_reason_code(reason_code, fallback="invalid_result")
        super().__init__(self.reason_code)


class _HistoryDependencyError(HistoryDataError):
    """Optional provider dependency is not installed."""

    reason_code = "missing_dependency"


class ADataHistoryError(HistoryDataError):
    """AData ETF history was unavailable or failed the local safety checks.

    The exception keeps the provider identity and a bounded public reason code
    so direct callers can distinguish this optional route from other sources
    without receiving vendor response text.
    """

    source = "adata"

    def __init__(self, reason_code: str = "provider_error") -> None:
        self.public_reason_code = _safe_reason_code(reason_code, fallback="provider_error")
        self.reason_code = self.public_reason_code
        super().__init__(f"AData ETF history failed ({self.public_reason_code})")


@dataclass(frozen=True, slots=True)
class _HistoryRequest:
    code: str
    asset_type: AssetType
    exchange: Exchange
    adjustment: str
    as_of: date | datetime
    start: date
    end: date
    cutoff: date
    explicit_start: bool
    explicit_end: bool


@dataclass(frozen=True, slots=True)
class _RawBar:
    date: date
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float


class FailoverHistoryProvider:
    """按配置顺序尝试 provider，并只返回一个 provider 的完整结果。"""

    name = "failover"

    def __init__(self, providers: Sequence[DailyHistoryProvider]) -> None:
        provider_tuple = tuple(providers)
        if not provider_tuple:
            raise ValueError("at least one history provider is required")
        self.providers = provider_tuple

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
        request = _history_request(
            code,
            asset_type=asset_type,
            as_of=as_of,
            start=start,
            end=end,
            adjustment=adjustment,
            exchange=exchange,
        )
        attempts: list[HistorySourceAttempt] = []
        for provider in self.providers:
            source = _provider_name(provider)
            try:
                result = provider.fetch_daily_bars(
                    request.code,
                    asset_type=request.asset_type,
                    as_of=request.as_of,
                    start=request.start,
                    end=request.end,
                    adjustment=request.adjustment,
                    exchange=request.exchange,
                )
                _validate_history_result(result, request, source=source)
            except HistoryUnsupportedError as exc:
                attempts.append(
                    _attempt(source, "unsupported", getattr(exc, "public_reason_code", None))
                )
                continue
            except _InvalidHistoryResult as exc:
                attempts.append(_attempt(source, "failed", exc.reason_code))
                continue
            except Exception as exc:  # noqa: BLE001 - bounded provider boundary
                attempts.append(_attempt(source, "failed", _exception_reason(exc)))
                continue

            attempts.append(_attempt(source, "success", "ok"))
            # ``replace`` keeps the bars object exactly as returned by the winning
            # provider. It only adds safe attempt metadata; no source is merged.
            return replace(result, source_attempts=tuple(attempts))

        raise HistoryFailoverError(tuple(attempts))

    fetch_daily_history = fetch_daily_bars


class ADataEtfHistoryProvider:
    """通过 AData 的同花顺 ETF 日线接口获取 ETF 历史日线。

    AData 是可选依赖，且只在实际 ETF 请求时导入。它不是股票线路：股票
    请求在任何导入或网络动作前直接返回 ``unsupported``。同花顺日线接口
    的路径约定为 ``00`` 不复权、``01`` 前复权、``02`` 后复权；AData 公共
    方法只暴露前复权，因此 ``none``/``hfq`` 走同一个 AData THS 客户端的
    原始日线请求，并在本地解析为统一字段。

    ``frame_fetcher`` 仅供测试和离线验证注入，调用参数模拟 AData ETF 日线
    入口：``fund_code``、``k_type``、``start_date``、``end_date``、
    ``adjustment`` 与 ``path``。
    """

    name = "adata"
    source_url = "https://github.com/1nchaos/adata"
    _FIELDS = ("trade_date", "open", "high", "low", "close", "volume", "amount")
    _ADJUSTMENT_PATH: ClassVar[dict[str, str]] = {
        "none": "00",
        "qfq": "01",
        "hfq": "02",
    }

    def __init__(
        self,
        *,
        limit: int = _DEFAULT_LIMIT,
        timeout_seconds: float = _DEFAULT_ADATA_TIMEOUT_SECONDS,
        frame_fetcher: Callable[..., object] | None = None,
        fetcher: Callable[..., object] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if limit <= 0:
            raise ValueError("limit must be positive")
        if (
            isinstance(timeout_seconds, bool)
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
            or timeout_seconds > 30.0
        ):
            raise ValueError("timeout_seconds must be finite, positive, and at most 30 seconds")
        if frame_fetcher is not None and fetcher is not None:
            raise ValueError("provide only one frame fetcher")
        self._limit = limit
        self.timeout_seconds = float(timeout_seconds)
        self._frame_fetcher = frame_fetcher or fetcher
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
        # This check must precede request normalization and optional imports: a
        # stock must never cause an AData network request or dependency lookup.
        if _normalize_asset_type(asset_type) != "etf":
            raise HistoryUnsupportedError()
        request = _history_request(
            code,
            asset_type=asset_type,
            as_of=as_of,
            start=start,
            end=end,
            adjustment=adjustment,
            exchange=exchange,
            default_limit=self._limit,
        )
        try:
            frame = self._fetch_frame(request)
            records = _records_from_frame(frame, fields=self._FIELDS)
            raw_bars, dropped = _parse_adata_raw_bars(
                records,
                fields=self._FIELDS,
                request=request,
            )
            if not raw_bars:
                raise ADataHistoryError("empty_result")
            return _history_from_raw_bars(
                raw_bars,
                dropped=dropped,
                raw_bar_count=len(records),
                request=request,
                source=self.name,
                source_url=self._source_url(request),
                now=self._now,
            )
        except ADataHistoryError:
            raise
        except _HistoryDependencyError:
            raise ADataHistoryError("missing_dependency") from None
        except HistoryDataError as exc:
            raise ADataHistoryError(_history_error_reason(exc)) from None
        except Exception:  # noqa: BLE001 - vendor boundary is deliberately bounded
            raise ADataHistoryError("provider_error") from None

    fetch_daily_history = fetch_daily_bars

    def _fetch_frame(self, request: _HistoryRequest) -> object:
        path = self._ADJUSTMENT_PATH[request.adjustment]
        kwargs = {
            "fund_code": request.code,
            "k_type": 1,
            "start_date": request.start.strftime("%Y-%m-%d"),
            "end_date": request.end.strftime("%Y-%m-%d"),
            "adjustment": request.adjustment,
            "path": path,
        }
        if self._frame_fetcher is not None:
            return self._frame_fetcher(**kwargs)
        worker = Path(__file__).with_name("_adata_worker.py")
        command = [
            sys.executable,
            str(worker),
            "--code",
            request.code,
            "--start",
            request.start.isoformat(),
            "--end",
            request.cutoff.isoformat(),
            "--adjustment",
            request.adjustment,
        ]
        run_kwargs: dict[str, object] = {
            "shell": False,
            "capture_output": True,
            "text": True,
            "timeout": self.timeout_seconds,
        }
        if sys.platform == "win32":
            # Keep the helper invisible when the desktop app invokes it on
            # Windows; no console window should appear for a data request.
            run_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            completed = subprocess.run(command, check=False, **run_kwargs)
        except subprocess.TimeoutExpired:
            # subprocess.run terminates and waits for the child on timeout.
            raise ADataHistoryError("timeout") from None
        except OSError:
            raise ADataHistoryError("provider_error") from None
        if completed.returncode != 0:
            raise ADataHistoryError("provider_error")
        try:
            payload = json.loads(completed.stdout or "")
        except (TypeError, ValueError, json.JSONDecodeError):
            raise ADataHistoryError("invalid_response") from None
        if not isinstance(payload, Mapping):
            raise ADataHistoryError("invalid_response")
        if payload.get("ok") is not True:
            reason = payload.get("reason_code")
            raise ADataHistoryError(_safe_reason_code(reason, fallback="provider_error"))
        rows = payload.get("rows")
        if not isinstance(rows, list):
            raise ADataHistoryError("invalid_response")
        return rows

    def _source_url(self, request: _HistoryRequest) -> str:
        path = self._ADJUSTMENT_PATH[request.adjustment]
        return f"http://d.10jqka.com.cn/v6/line/hs_{request.code}/{path}/last36000.js"


class TencentHistoryProvider:
    """通过 AKShare ``stock_zh_a_hist_tx`` 获取腾讯股票日线。

    腾讯该接口只在本 provider 中声明支持股票；ETF 请求明确返回
    :class:`HistoryUnsupportedError`。AKShare 和其依赖均在首次请求时加载。
    """

    name = "tencent"
    source_url = "https://akshare.akfamily.xyz/data/stock/stock.html#stock-zh-a-hist-tx"
    _FIELDS = ("date", "open", "high", "low", "close", "volume", "amount")

    def __init__(
        self,
        *,
        limit: int = _DEFAULT_LIMIT,
        frame_fetcher: Callable[..., object] | None = None,
        fetcher: Callable[..., object] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if limit <= 0:
            raise ValueError("limit must be positive")
        if frame_fetcher is not None and fetcher is not None:
            raise ValueError("provide only one frame fetcher")
        self._limit = limit
        self._frame_fetcher = frame_fetcher or fetcher
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
        if _normalize_asset_type(asset_type) != "stock":
            raise HistoryUnsupportedError()
        request = _history_request(
            code,
            asset_type=asset_type,
            as_of=as_of,
            start=start,
            end=end,
            adjustment=adjustment,
            exchange=exchange,
            default_limit=self._limit,
        )
        frame = self._fetch_frame(request)
        records = _records_from_frame(frame)
        return _history_from_records(
            records,
            fields=self._FIELDS,
            request=request,
            source=self.name,
            source_url=self.source_url,
            now=self._now,
        )

    fetch_daily_history = fetch_daily_bars

    def _fetch_frame(self, request: _HistoryRequest) -> object:
        kwargs = {
            "symbol": request.code,
            "start_date": request.start.strftime("%Y%m%d"),
            "end_date": request.end.strftime("%Y%m%d"),
            "adjust": {"none": "", "qfq": "qfq", "hfq": "hfq"}[request.adjustment],
        }
        if self._frame_fetcher is not None:
            return self._frame_fetcher(**kwargs)
        try:
            ak = importlib.import_module("akshare")
            fetch = ak.stock_zh_a_hist_tx
        except (ImportError, AttributeError):
            raise _HistoryDependencyError("Tencent history dependency is unavailable") from None
        return fetch(**kwargs)


class BaoStockHistoryProvider:
    """通过 BaoStock ``query_history_k_data_plus`` 获取股票日线。"""

    name = "baostock"
    source_url = "https://www.baostock.com/"
    _QUERY_FIELDS = (
        "date",
        "code",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amount",
        "adjustflag",
    )
    _FIELDS = _QUERY_FIELDS
    _ADJUST_FLAGS: ClassVar[dict[str, str]] = {"none": "3", "qfq": "2", "hfq": "1"}

    def __init__(
        self,
        module: object | None = None,
        *,
        baostock_module: object | None = None,
        timeout_seconds: float = _DEFAULT_BAOSTOCK_TIMEOUT_SECONDS,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if module is not None and baostock_module is not None:
            raise ValueError("provide only one BaoStock module")
        if isinstance(timeout_seconds, bool) or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be a positive finite number")
        self._module = module if module is not None else baostock_module
        self.timeout_seconds = float(timeout_seconds)
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
        if _normalize_asset_type(asset_type) != "stock":
            raise HistoryUnsupportedError()
        request = _history_request(
            code,
            asset_type=asset_type,
            as_of=as_of,
            start=start,
            end=end,
            adjustment=adjustment,
            exchange=exchange,
            default_limit=_DEFAULT_LIMIT,
        )
        module = self._load_module()
        with _BAOSTOCK_LOCK:
            previous_timeout = socket.getdefaulttimeout()
            timeout_set = False
            try:
                socket.setdefaulttimeout(self.timeout_seconds)
                timeout_set = True
                try:
                    login_result = module.login()  # type: ignore[attr-defined]
                    _ensure_baostock_success(login_result)
                    symbol = f"{request.exchange}.{request.code}"
                    query_result = module.query_history_k_data_plus(  # type: ignore[attr-defined]
                        symbol,
                        ",".join(self._QUERY_FIELDS),
                        start_date=request.start.strftime("%Y-%m-%d"),
                        end_date=request.end.strftime("%Y-%m-%d"),
                        frequency="d",
                        adjustflag=self._ADJUST_FLAGS[request.adjustment],
                    )
                    _ensure_baostock_success(query_result)
                    fields = _response_fields(query_result) or self._QUERY_FIELDS
                    records = _baostock_records(query_result, fields=fields)
                    _validate_baostock_rows(
                        records,
                        fields=fields,
                        expected_code=symbol,
                        expected_adjustflag=self._ADJUST_FLAGS[request.adjustment],
                    )
                    return _history_from_records(
                        records,
                        fields=fields,
                        request=request,
                        source=self.name,
                        source_url=self.source_url,
                        now=self._now,
                    )
                finally:
                    # BaoStock requires logout even when login/query/parsing raises.
                    logout = getattr(module, "logout", None)
                    if callable(logout):
                        active_exception = sys.exc_info()[0] is not None
                        try:
                            logout()
                        except Exception:  # noqa: BLE001 - do not leak vendor errors
                            if not active_exception:
                                raise HistoryDataError("BaoStock logout failed") from None
            finally:
                if timeout_set:
                    socket.setdefaulttimeout(previous_timeout)

    fetch_daily_history = fetch_daily_bars

    def _load_module(self) -> object:
        if self._module is not None:
            return self._module
        try:
            return importlib.import_module("baostock")
        except ImportError:
            raise _HistoryDependencyError("BaoStock history dependency is unavailable") from None


def build_default_history_provider(
    additional_providers: Sequence[DailyHistoryProvider] = (),
) -> FailoverHistoryProvider:
    """构造默认线路：东方财富、可选 Tushare、AData ETF、腾讯、BaoStock。"""

    providers: list[DailyHistoryProvider] = [EastmoneyHistoryProvider()]
    providers.extend(tuple(additional_providers))
    providers.extend((ADataEtfHistoryProvider(), TencentHistoryProvider(), BaoStockHistoryProvider()))
    return FailoverHistoryProvider(providers)


def _history_request(
    code: str,
    *,
    asset_type: AssetType,
    as_of: date | datetime | None,
    start: date | datetime | None,
    end: date | datetime | None,
    adjustment: str,
    exchange: Exchange | None,
    default_limit: int = _DEFAULT_LIMIT,
) -> _HistoryRequest:
    normalized_code = _normalize_code(code)
    normalized_asset_type = _normalize_asset_type(asset_type)
    normalized_adjustment = _normalize_adjustment(adjustment)
    normalized_exchange = infer_exchange(
        normalized_code,
        asset_type=normalized_asset_type,
        exchange=exchange,
    )
    requested_as_of = as_of if as_of is not None else datetime.now(_SHANGHAI_TZ)
    if not isinstance(requested_as_of, (date, datetime)):
        raise TypeError("as_of must be a date or datetime")
    requested_end = _as_date(end) if end is not None else _as_date(requested_as_of)
    requested_start = (
        _as_date(start)
        if start is not None
        else requested_end - timedelta(days=default_limit * 3)
    )
    if requested_start > requested_end:
        raise ValueError("start cannot be later than end")
    cutoff = min(requested_end, _completed_cutoff(requested_as_of))
    return _HistoryRequest(
        code=normalized_code,
        asset_type=normalized_asset_type,
        exchange=normalized_exchange,
        adjustment=normalized_adjustment,
        as_of=requested_as_of,
        start=requested_start,
        end=requested_end,
        cutoff=cutoff,
        explicit_start=start is not None,
        explicit_end=end is not None,
    )


def _validate_history_result(
    result: object,
    request: _HistoryRequest,
    *,
    source: str,
) -> None:
    if not isinstance(result, HistoryData):
        raise _InvalidHistoryResult("invalid_result")
    if str(result.source).strip().lower() != source:
        raise _InvalidHistoryResult("invalid_result")
    if result.code != request.code:
        raise _InvalidHistoryResult("request_error")
    if result.asset_type != request.asset_type:
        raise _InvalidHistoryResult("request_error")
    if str(result.exchange).strip().lower() != request.exchange:
        raise _InvalidHistoryResult("request_error")
    if str(result.adjustment).strip().lower() != request.adjustment:
        raise _InvalidHistoryResult("request_error")
    if result.amount_unit != "CNY":
        raise _InvalidHistoryResult("invalid_result")
    if result.volume_unit not in {"shares", "source_native"}:
        raise _InvalidHistoryResult("invalid_result")
    if result.complete is not True:
        raise _InvalidHistoryResult("invalid_result")
    if not isinstance(result.requested_as_of, (date, datetime)):
        raise _InvalidHistoryResult("invalid_result")
    if not isinstance(result.requested_start, date) or isinstance(result.requested_start, datetime):
        raise _InvalidHistoryResult("invalid_result")
    if not isinstance(result.requested_end, date) or isinstance(result.requested_end, datetime):
        raise _InvalidHistoryResult("invalid_result")
    if request.explicit_start and result.requested_start != request.start:
        raise _InvalidHistoryResult("request_error")
    if request.explicit_end and result.requested_end != request.end:
        raise _InvalidHistoryResult("request_error")
    bars = result.bars
    if not isinstance(bars, tuple) or not bars:
        raise _InvalidHistoryResult("empty_result")
    dates: list[date] = []
    for bar in bars:
        if not isinstance(bar, HistoryBar) or not isinstance(bar.date, date):
            raise _InvalidHistoryResult("invalid_result")
        try:
            _validate_bar(bar, request.code)
        except Exception:  # noqa: BLE001 - normalize all bar failures
            raise _InvalidHistoryResult("invalid_result") from None
        dates.append(bar.date)
    if any(current >= following for current, following in pairwise(dates)):
        raise _InvalidHistoryResult("invalid_result")
    if result.completed_through != dates[-1]:
        raise _InvalidHistoryResult("invalid_result")
    if any(day < request.start or day > request.end or day > request.cutoff for day in dates):
        raise _InvalidHistoryResult("invalid_result")


def _history_from_records(
    records: Sequence[object],
    *,
    fields: Sequence[str],
    request: _HistoryRequest,
    source: str,
    source_url: str,
    now: Callable[[], datetime],
) -> HistoryData:
    raw_bars, dropped = _parse_raw_bars(
        records,
        fields=fields,
        request=request,
        volume_scale=1.0,
        amount_scale=1.0,
    )
    return _history_from_raw_bars(
        raw_bars,
        dropped=dropped,
        raw_bar_count=len(records),
        request=request,
        source=source,
        source_url=source_url,
        now=now,
    )


def _history_from_raw_bars(
    raw_bars: Sequence[_RawBar],
    *,
    dropped: int,
    raw_bar_count: int,
    request: _HistoryRequest,
    source: str,
    source_url: str,
    now: Callable[[], datetime],
) -> HistoryData:
    if not raw_bars:
        raise HistoryDataError("history result is empty")
    bars: list[HistoryBar] = []
    for raw in raw_bars:
        bar = HistoryBar(
            date=raw.date,
            open=raw.open,
            high=raw.high,
            low=raw.low,
            close=raw.close,
            volume=raw.volume,
            amount=raw.amount,
        )
        try:
            _validate_bar(bar, request.code)
        except Exception as exc:
            raise HistoryDataError("history result contains unsafe OHLC or volume fields") from exc
        bars.append(bar)
    return HistoryData(
        code=request.code,
        asset_type=request.asset_type,
        exchange=request.exchange,
        secid=security_id(
            request.code,
            asset_type=request.asset_type,
            exchange=request.exchange,
        ),
        bars=tuple(bars),
        source=source,
        source_url=source_url,
        adjustment=request.adjustment,
        requested_as_of=request.as_of,
        requested_start=request.start,
        requested_end=request.end,
        completed_through=bars[-1].date,
        fetched_at=now(),
        raw_bar_count=raw_bar_count,
        dropped_bar_count=dropped,
        complete=True,
        volume_unit="shares",
        amount_unit="CNY",
    )


def _parse_adata_raw_bars(
    records: Sequence[object],
    *,
    fields: Sequence[str],
    request: _HistoryRequest,
) -> tuple[tuple[_RawBar, ...], int]:
    """Parse AData rows while dropping malformed/out-of-range rows safely.

    AData's upstream parser may return ``None``, ``--`` or other non-numeric
    values for individual rows. One bad row should not discard an otherwise
    usable daily history, but conflicting duplicate dates remain unsafe and
    reject the provider result.
    """

    selected: dict[date, _RawBar] = {}
    dropped = 0
    for row in records:
        try:
            row_date = _parse_date(_record_value(row, ("date", "trade_date"), fields))
        except HistoryDataError:
            dropped += 1
            continue
        if row_date < request.start or row_date > request.end or row_date > request.cutoff:
            dropped += 1
            continue
        try:
            raw = _RawBar(
                date=row_date,
                open=_number(_record_value(row, ("open",), fields)),
                high=_number(_record_value(row, ("high",), fields)),
                low=_number(_record_value(row, ("low",), fields)),
                close=_number(_record_value(row, ("close",), fields)),
                volume=_number(_record_value(row, ("volume", "vol"), fields)),
                amount=_number(_record_value(row, ("amount",), fields)),
            )
        except HistoryDataError:
            dropped += 1
            continue
        previous = selected.get(row_date)
        if previous is not None:
            if previous != raw:
                raise ADataHistoryError("invalid_result")
            dropped += 1
            continue
        selected[row_date] = raw
    return tuple(selected[day] for day in sorted(selected)), dropped


def _history_error_reason(exc: HistoryDataError) -> str:
    reason = getattr(exc, "reason_code", None)
    if reason is not None:
        return _safe_reason_code(reason, fallback="invalid_result")
    message = str(exc).lower()
    if "missing" in message:
        return "missing_field"
    if "empty" in message:
        return "empty_result"
    if "response" in message:
        return "invalid_response"
    return "invalid_result"


def _parse_raw_bars(
    records: Sequence[object],
    *,
    fields: Sequence[str],
    request: _HistoryRequest,
    volume_scale: float,
    amount_scale: float,
) -> tuple[tuple[_RawBar, ...], int]:
    selected: list[_RawBar] = []
    seen: set[date] = set()
    dropped = 0
    for row in records:
        row_date = _parse_date(_record_value(row, ("date", "trade_date"), fields))
        if row_date < request.start or row_date > request.end or row_date > request.cutoff:
            dropped += 1
            continue
        if row_date in seen:
            raise HistoryDataError("history result contains duplicate dates")
        seen.add(row_date)
        raw = _RawBar(
            date=row_date,
            open=_number(_record_value(row, ("open",), fields)),
            high=_number(_record_value(row, ("high",), fields)),
            low=_number(_record_value(row, ("low",), fields)),
            close=_number(_record_value(row, ("close",), fields)),
            volume=_number(_record_value(row, ("volume", "vol"), fields)) * volume_scale,
            amount=_number(_record_value(row, ("amount",), fields)) * amount_scale,
        )
        selected.append(raw)
    selected.sort(key=lambda item: item.date)
    return tuple(selected), dropped


def _records_from_frame(frame: object, *, fields: Sequence[str] = ()) -> list[object]:
    if frame is None:
        return []
    if hasattr(frame, "to_dict"):
        to_dict = frame.to_dict
        try:
            rows = to_dict(orient="records")
        except TypeError:
            rows = to_dict()
        if isinstance(rows, list):
            return rows
        if isinstance(rows, Mapping):
            frame = rows
    if isinstance(frame, Mapping):
        if isinstance(frame.get("data"), (list, tuple)):
            return list(frame["data"])
        if isinstance(frame.get("items"), (list, tuple)):
            return list(frame["items"])
        values = list(frame.values())
        if values and all(isinstance(value, Sequence) and not isinstance(value, str) for value in values):
            length = max(len(value) for value in values)
            keys = list(frame)
            return [
                {key: frame[key][index] if index < len(frame[key]) else None for key in keys}
                for index in range(length)
            ]
        return [frame]
    if isinstance(frame, Sequence) and not isinstance(frame, (str, bytes, bytearray)):
        return list(frame)
    raise HistoryDataError("history response is not tabular")


def _record_value(row: object, aliases: Sequence[str], fields: Sequence[str]) -> object:
    normalized_aliases = {alias.strip().lower() for alias in aliases}
    if isinstance(row, Mapping):
        values = {str(key).strip().lower(): value for key, value in row.items()}
        for alias in normalized_aliases:
            if alias in values:
                return values[alias]
        raise HistoryDataError("history response is missing a required field")
    if isinstance(row, Sequence) and not isinstance(row, (str, bytes, bytearray)):
        normalized_fields = [str(field).strip().lower() for field in fields]
        for alias in normalized_aliases:
            if alias in normalized_fields:
                index = normalized_fields.index(alias)
                if index < len(row):
                    return row[index]
        raise HistoryDataError("history response is missing a required field")
    raise HistoryDataError("history response has an invalid row")


def _parse_date(value: object) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if hasattr(value, "date") and callable(value.date):
        candidate = value.date()
        if isinstance(candidate, date) and not isinstance(candidate, datetime):
            return candidate
    text = str(value or "").strip()
    if re.fullmatch(r"\d{8}", text):
        try:
            return date(int(text[:4]), int(text[4:6]), int(text[6:]))
        except ValueError:
            pass
    parts = re.split(r"[-/.]", text)
    if len(parts) == 3 and all(part.isdigit() for part in parts):
        try:
            return date(int(parts[0]), int(parts[1]), int(parts[2]))
        except ValueError:
            pass
    raise HistoryDataError("history response contains an invalid date")


def _number(value: object) -> float:
    if value is None or isinstance(value, bool):
        raise HistoryDataError("history response contains a missing number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise HistoryDataError("history response contains an invalid number") from exc
    if not math.isfinite(result):
        raise HistoryDataError("history response contains a non-finite number")
    return result


def _response_fields(response: object) -> tuple[str, ...]:
    value = getattr(response, "fields", None)
    if value is None and isinstance(response, Mapping):
        value = response.get("fields")
    if isinstance(value, str):
        return tuple(item.strip() for item in value.split(",") if item.strip())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(str(item) for item in value)
    return ()


def _response_data(response: object) -> object:
    get_data = getattr(response, "get_data", None)
    if callable(get_data):
        return get_data()
    if isinstance(response, Mapping) and "data" in response:
        return response["data"]
    if hasattr(response, "data"):
        return response.data
    return response


def _baostock_records(response: object, *, fields: Sequence[str]) -> list[object]:
    """Read real BaoStock rows first, with a tabular compatibility fallback."""

    next_row = getattr(response, "next", None)
    get_row_data = getattr(response, "get_row_data", None)
    if callable(next_row) and callable(get_row_data):
        rows: list[object] = []
        try:
            while next_row():
                _ensure_baostock_success(response)
                rows.append(get_row_data())
                _ensure_baostock_success(response)
            _ensure_baostock_success(response)
            return rows
        except AttributeError:
            # A few test doubles expose the real method names but only provide
            # the legacy tabular accessor; keep that compatibility path bounded.
            pass
    return _records_from_frame(_response_data(response), fields=fields)


def _validate_baostock_rows(
    records: Sequence[object],
    *,
    fields: Sequence[str],
    expected_code: str,
    expected_adjustflag: str,
) -> None:
    normalized_expected_code = expected_code.strip().lower()
    for row in records:
        value = _record_value(row, ("code",), fields)
        actual_code = str(value or "").strip().lower()
        if actual_code != normalized_expected_code:
            raise HistoryDataError("BaoStock row code does not match the request")
        value = _record_value(row, ("adjustflag",), fields)
        actual_adjustflag = str(value or "").strip()
        actual_adjustflag = actual_adjustflag.removesuffix(".0")
        if actual_adjustflag != expected_adjustflag:
            raise HistoryDataError("BaoStock row adjustment flag does not match the request")


def _ensure_baostock_success(response: object) -> None:
    code = _response_code(response)
    if code is not None and code not in {"0", "0.0"}:
        raise HistoryDataError("BaoStock response was rejected")


def _response_code(response: object) -> str | None:
    if response is None:
        return "0"
    value = getattr(response, "error_code", None)
    if value is None and isinstance(response, Mapping):
        value = response.get("error_code", response.get("code"))
    if value is None:
        return "0"
    return str(value).strip()


def _provider_name(provider: object) -> str:
    try:
        value = str(getattr(provider, "name"))  # noqa: B009 - protocol attribute lookup
    except Exception:  # noqa: BLE001 - metadata boundary
        return "unknown"
    value = value.strip().lower()
    if re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,63}", value):
        return value
    return "unknown"


def _attempt(source: str, status: str, reason_code: str | None) -> HistorySourceAttempt:
    safe_status = status if status in {"success", "failed", "unsupported"} else "failed"
    return HistorySourceAttempt(
        source=_provider_name(type("ProviderName", (), {"name": source})()),
        status=safe_status,  # type: ignore[arg-type]
        reason_code=_safe_reason_code(reason_code, fallback="provider_error"),
    )


def _safe_reason_code(value: object, *, fallback: str) -> str:
    text = str(value or "").strip().lower()
    if text in _REASON_CODES:
        return text
    return fallback


def _exception_reason(exc: Exception) -> str:
    if isinstance(exc, ImportError):
        return "missing_dependency"
    if isinstance(exc, HistoryDataError):
        return _safe_reason_code(getattr(exc, "reason_code", None), fallback="invalid_result")
    return "provider_error"


__all__ = [
    "ADataEtfHistoryProvider",
    "ADataHistoryError",
    "BaoStockHistoryProvider",
    "DailyHistoryProvider",
    "FailoverHistoryProvider",
    "HistoryFailoverError",
    "HistoryUnsupportedError",
    "TencentHistoryProvider",
    "build_default_history_provider",
]
