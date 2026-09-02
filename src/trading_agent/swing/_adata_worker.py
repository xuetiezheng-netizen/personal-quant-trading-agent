"""隔离执行可选 AData ETF 日线请求的最小子进程入口。

这个文件只使用标准库。父进程只把公开 ETF 代码、日期和固定复权参数传入；
供应商依赖、网络请求及 DataFrame 都留在子进程内，stdout 只返回一个受限的
JSON 载荷。供应商自己的普通输出会重定向到 stderr，避免污染协议。
"""

from __future__ import annotations

import argparse
import contextlib
import importlib
import json
import math
import re
import sys
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any

_CODE_PATTERN = re.compile(r"^[0-9]{6}$")
_ADJUSTMENTS = {"none": "00", "qfq": "01", "hfq": "02"}
_MAX_ROWS = 5_000
_FIELDS = ("trade_date", "open", "high", "low", "close", "volume", "amount")


def main(argv: Sequence[str] | None = None) -> int:
    """Fetch one public ETF history and emit one safe JSON object."""

    try:
        arguments = _parse_arguments(argv)
        rows = _fetch_rows(
            code=arguments.code,
            start=arguments.start,
            end=arguments.end,
            adjustment=arguments.adjustment,
        )
        payload = {"ok": True, "rows": rows}
    except SystemExit:
        payload = {"ok": False, "reason_code": "invalid_response"}
    except ModuleNotFoundError:
        payload = {"ok": False, "reason_code": "missing_dependency"}
    except ImportError:
        payload = {"ok": False, "reason_code": "missing_dependency"}
    except _InvalidResponse:
        payload = {"ok": False, "reason_code": "invalid_response"}
    except Exception:  # noqa: BLE001 - vendor boundary must not leak details
        payload = {"ok": False, "reason_code": "provider_error"}
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return 0


class _InvalidResponse(Exception):
    """Internal marker for a malformed vendor response."""


def _parse_arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--code", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--adjustment", required=True)
    arguments = parser.parse_args(argv)
    if not _CODE_PATTERN.fullmatch(arguments.code):
        raise _InvalidResponse
    if arguments.adjustment not in _ADJUSTMENTS:
        raise _InvalidResponse
    try:
        start = date.fromisoformat(arguments.start)
        end = date.fromisoformat(arguments.end)
    except ValueError as exc:
        raise _InvalidResponse from exc
    if start > end:
        raise _InvalidResponse
    arguments.start = start
    arguments.end = end
    return arguments


def _fetch_rows(*, code: str, start: date, end: date, adjustment: str) -> list[dict[str, Any]]:
    """Run AData and turn its result into bounded JSON-compatible records."""

    with contextlib.redirect_stdout(sys.stderr):
        module = importlib.import_module("adata.fund.market.etf_market_ths")
        market = module.ETFMarketThs()
        if adjustment == "qfq":
            frame = market.get_market_etf_ths(
                fund_code=code,
                k_type=1,
                start_date=start.isoformat(),
                end_date=end.isoformat(),
            )
            records = _records_from_frame(frame)
        else:
            path = _ADJUSTMENTS[adjustment]
            url = f"http://d.10jqka.com.cn/v6/line/hs_{code}/{path}/last36000.js"
            text = market._get_text(url, code)
            records = _records_from_text(text)
        return _filter_records(records, start=start, end=end)


def _records_from_frame(frame: object) -> list[object]:
    if frame is None:
        return []
    to_dict = getattr(frame, "to_dict", None)
    if callable(to_dict):
        try:
            rows = to_dict(orient="records")
        except TypeError:
            rows = to_dict()
        if isinstance(rows, list):
            return rows
        if isinstance(rows, Mapping):
            frame = rows
    if isinstance(frame, Mapping):
        data = frame.get("data")
        if isinstance(data, (list, tuple)):
            return list(data)
        items = frame.get("items")
        if isinstance(items, (list, tuple)):
            return list(items)
        return [frame]
    if isinstance(frame, Sequence) and not isinstance(frame, (str, bytes, bytearray)):
        return list(frame)
    raise _InvalidResponse


def _records_from_text(text: object) -> list[dict[str, str]]:
    if not isinstance(text, str) or not text.strip():
        raise _InvalidResponse
    try:
        payload_text = text[text.index("{") :].rstrip(" );\r\n")
        payload = json.loads(payload_text)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise _InvalidResponse from exc
    if not isinstance(payload, Mapping):
        raise _InvalidResponse
    if str(payload.get("total", "1")) == "0":
        return []
    data = payload.get("data")
    if not isinstance(data, str):
        raise _InvalidResponse
    records: list[dict[str, str]] = []
    for raw_row in data.split(";"):
        values = raw_row.split(",")
        if len(values) >= len(_FIELDS):
            records.append(dict(zip(_FIELDS, values[: len(_FIELDS)])))
    return records


def _filter_records(records: Sequence[object], *, start: date, end: date) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for row in records:
        if not isinstance(row, Mapping):
            continue
        row_date = _parse_date(row.get("trade_date", row.get("date")))
        if row_date is None or row_date < start or row_date > end:
            continue
        normalized: dict[str, Any] = {"trade_date": row_date.isoformat()}
        for field in _FIELDS[1:]:
            normalized[field] = _json_value(row.get(field))
        selected.append(normalized)
    selected.sort(key=lambda row: row["trade_date"])
    return selected[-_MAX_ROWS:]


def _parse_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if re.fullmatch(r"\d{8}", text):
        try:
            return date(int(text[:4]), int(text[4:6]), int(text[6:]))
        except ValueError:
            return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text[:10]):
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None
    return None


def _json_value(value: object) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return str(value)
    return value


if __name__ == "__main__":
    raise SystemExit(main())
