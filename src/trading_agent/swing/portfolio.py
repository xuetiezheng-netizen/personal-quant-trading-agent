"""本机私有持仓的最小 JSON 存储层。

这个模块只保存用户在本机维护的持仓元数据，不把持仓写入代码库或公开
报告。一个 ``Holding`` 代表一条完整持仓；其中 ``tactical_ratio`` 仅是
历史模拟时可编辑的假设，核心仓比例由 ``1 - tactical_ratio`` 推导，策略
本身不会改写核心仓。

文件格式刻意保持简单，方便用户备份和人工检查。所有更新都先写入同目录
临时文件并完成 flush/fsync，再用 ``os.replace`` 替换旧文件；读取失败时
绝不会把损坏文件自动清空。
"""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal

SCHEMA_VERSION = 1
DEFAULT_HOLDINGS_PATH = Path("data") / "private" / "holdings.json"
AssetType = Literal["stock", "etf"]

_CODE_PATTERN = re.compile(r"^[0-9]{6}$")
_TOP_LEVEL_FIELDS = frozenset({"schema_version", "revision", "updated_at", "holdings"})
_HOLDING_FIELDS = frozenset(
    {
        "code",
        "name",
        "asset_type",
        "quantity",
        "avg_cost_cny",
        "acquired_date",
        "note",
        "revision",
        "tactical_ratio",
    }
)
_REQUIRED_HOLDING_FIELDS = frozenset(
    {"code", "name", "asset_type", "quantity", "avg_cost_cny"}
)
_UPDATE_FIELDS = _HOLDING_FIELDS
_MAX_NAME_LENGTH = 100
_MAX_NOTE_LENGTH = 500


class PortfolioError(Exception):
    """持仓存储层的基类异常。"""


class PortfolioValidationError(PortfolioError, ValueError):
    """输入或文件结构不符合持仓 schema。"""


class PortfolioRevisionConflictError(PortfolioError):
    """调用方持有的版本已经过期。"""

    def __init__(self, expected_revision: int, actual_revision: int) -> None:
        self.expected_revision = expected_revision
        self.actual_revision = actual_revision
        super().__init__(
            f"持仓数据已被更新，请刷新后重试（期望版本 {expected_revision}，当前版本 {actual_revision}）"
        )


class PortfolioStorageError(PortfolioError):
    """本机私有文件无法读取或安全保存。"""


class PortfolioCorruptionError(PortfolioStorageError):
    """持仓文件已损坏或不是支持的 JSON schema。"""


# 这些别名让上层服务可以采用更短的命名，同时保持一个明确的异常类型。
RevisionConflictError = PortfolioRevisionConflictError
CorruptPortfolioError = PortfolioCorruptionError


def _validation_error(field: str, reason: str) -> PortfolioValidationError:
    """构造不包含文件路径、原始个人值的中文校验错误。"""

    return PortfolioValidationError(f"{field}{reason}")


def _is_finite_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _validate_code(value: object) -> str:
    if not isinstance(value, str) or _CODE_PATTERN.fullmatch(value) is None:
        raise _validation_error("证券代码", "必须是6位数字")
    return value


def _validate_asset_type(value: object) -> AssetType:
    if value not in ("stock", "etf"):
        raise _validation_error("资产类型", "只能是 stock 或 etf")
    return value  # type: ignore[return-value]


def _validate_name(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _validation_error("名称", "不能为空")
    if len(value) > _MAX_NAME_LENGTH:
        raise _validation_error("名称", f"长度不能超过{_MAX_NAME_LENGTH}个字符")
    return value


def _validate_quantity(value: object) -> int:
    if type(value) is not int or value <= 0:
        raise _validation_error("持仓数量", "必须是正整数")
    return value


def _validate_avg_cost(value: object) -> float:
    if not _is_finite_number(value) or float(value) <= 0:
        raise _validation_error("持仓成本", "必须是有限正数")
    return float(value)


def _validate_date(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _validation_error("建仓日期", "必须是 YYYY-MM-DD 日期")
    if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value) is None:
        raise _validation_error("建仓日期", "必须是 YYYY-MM-DD 日期")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise _validation_error("建仓日期", "必须是 YYYY-MM-DD 日期") from exc
    if parsed > datetime.now(UTC).date():
        raise _validation_error("建仓日期", "不能晚于今天")
    # fromisoformat accepts only a canonical calendar date for this input.  Keep
    # the user's string only after checking it so the JSON stays deterministic.
    return value


def _validate_note(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _validation_error("备注", "必须是文字")
    if len(value) > _MAX_NOTE_LENGTH:
        raise _validation_error("备注", f"长度不能超过{_MAX_NOTE_LENGTH}个字符")
    return value


def _validate_revision(value: object, *, required: bool = False) -> int | None:
    if value is None and not required:
        return None
    if type(value) is not int or value < 0:
        raise _validation_error("版本号", "必须是非负整数")
    return value


def _validate_tactical_ratio(value: object) -> float:
    if not _is_finite_number(value):
        raise _validation_error("机动仓模拟比例", "必须是有限数字")
    ratio = float(value)
    if not 0.0 <= ratio <= 0.5:
        raise _validation_error("机动仓模拟比例", "必须在0到0.5之间")
    return ratio


def _validate_mapping_fields(value: Mapping[str, object], allowed: frozenset[str], scope: str) -> None:
    if any(not isinstance(key, str) for key in value):
        raise _validation_error(scope, "字段名必须是文字")
    if set(value) - allowed:
        # 不回显未知字段名，避免把文件内容或用户备注拼进错误提示。
        raise _validation_error(scope, "包含不支持的字段")


@dataclass(frozen=True, slots=True)
class Holding:
    """一条完整的用户持仓记录。

    ``tactical_ratio`` 是历史回放的可编辑假设，默认值 0.2 不是仓位建议。
    ``core_ratio`` 由它派生，不在文件中另存一条“核心仓”，从而避免把同
    一资产拆成两条而造成误解。
    """

    code: str
    name: str
    asset_type: AssetType
    quantity: int
    avg_cost_cny: float
    acquired_date: str | None = None
    note: str | None = None
    revision: int | None = None
    tactical_ratio: float = 0.2

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", _validate_code(self.code))
        object.__setattr__(self, "name", _validate_name(self.name))
        object.__setattr__(self, "asset_type", _validate_asset_type(self.asset_type))
        object.__setattr__(self, "quantity", _validate_quantity(self.quantity))
        object.__setattr__(self, "avg_cost_cny", _validate_avg_cost(self.avg_cost_cny))
        object.__setattr__(self, "acquired_date", _validate_date(self.acquired_date))
        object.__setattr__(self, "note", _validate_note(self.note))
        object.__setattr__(self, "revision", _validate_revision(self.revision))
        object.__setattr__(self, "tactical_ratio", _validate_tactical_ratio(self.tactical_ratio))

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> Holding:
        _validate_mapping_fields(value, _HOLDING_FIELDS, "持仓记录")
        if _REQUIRED_HOLDING_FIELDS - set(value):
            raise _validation_error("持仓记录", "缺少必要字段")
        if "tactical_ratio" not in value:
            value = {**value, "tactical_ratio": 0.2}
        return cls(**value)  # type: ignore[arg-type]

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "code": self.code,
            "name": self.name,
            "asset_type": self.asset_type,
            "quantity": self.quantity,
            "avg_cost_cny": self.avg_cost_cny,
            "tactical_ratio": self.tactical_ratio,
        }
        if self.acquired_date is not None:
            result["acquired_date"] = self.acquired_date
        if self.note is not None:
            result["note"] = self.note
        if self.revision is not None:
            result["revision"] = self.revision
        return result

    @property
    def core_ratio(self) -> float:
        """由模拟比例推导的核心仓比例；策略不得据此改写真实数量。"""

        return 1.0 - self.tactical_ratio


@dataclass(frozen=True, slots=True)
class PortfolioSnapshot:
    """持仓文件的内存表示。"""

    schema_version: int = SCHEMA_VERSION
    revision: int = 0
    updated_at: str | None = None
    holdings: tuple[Holding, ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise _validation_error("schema_version", "不受支持")
        _validate_revision(self.revision, required=True)
        if self.updated_at is not None:
            _validate_updated_at(self.updated_at)
        _validate_unique_holdings(self.holdings)

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> PortfolioSnapshot:
        _validate_mapping_fields(value, _TOP_LEVEL_FIELDS, "持仓文件")
        for field in _TOP_LEVEL_FIELDS:
            if field not in value:
                raise _validation_error("持仓文件", "缺少必要字段")
        if value["schema_version"] != SCHEMA_VERSION:
            raise _validation_error("schema_version", "不受支持")
        revision = _validate_revision(value["revision"], required=True)
        updated_at = _validate_updated_at(value["updated_at"])
        raw_holdings = value["holdings"]
        if not isinstance(raw_holdings, list):
            raise _validation_error("持仓列表", "必须是数组")
        holdings = tuple(
            Holding.from_mapping(item) if isinstance(item, Mapping) else _invalid_holding_value()
            for item in raw_holdings
        )
        return cls(
            schema_version=SCHEMA_VERSION,
            revision=revision if revision is not None else 0,
            updated_at=updated_at,
            holdings=holdings,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "revision": self.revision,
            "updated_at": self.updated_at,
            "holdings": [holding.to_dict() for holding in self.holdings],
        }

    # 上层 HTTP 层通常更喜欢这个名字；返回值仍然是新建的普通 dict。
    def as_dict(self) -> dict[str, object]:
        return self.to_dict()


def _invalid_holding_value() -> Holding:
    raise _validation_error("持仓记录", "必须是对象")


def _validate_updated_at(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise _validation_error("更新时间", "必须是 ISO 时间")
    try:
        datetime.fromisoformat(value)
    except ValueError as exc:
        raise _validation_error("更新时间", "必须是 ISO 时间") from exc
    return value


def _holding_key(holding: Holding) -> tuple[AssetType, str]:
    return holding.asset_type, holding.code


def _validate_unique_holdings(holdings: Iterable[Holding]) -> None:
    seen: set[tuple[AssetType, str]] = set()
    for holding in holdings:
        if not isinstance(holding, Holding):
            raise _validation_error("持仓列表", "包含无效记录")
        key = _holding_key(holding)
        if key in seen:
            raise _validation_error("持仓列表", "同一资产不能重复")
        seen.add(key)


def _reject_json_constant(value: str) -> None:
    raise ValueError(value)


class PortfolioStore:
    """固定服务端本机路径的 JSON 持仓仓库。

    默认路径是相对于服务端工作目录的 ``data/private/holdings.json``。单元
    测试或调用方可以传入一个明确文件路径；Web 层不应把浏览器提交的任意
    路径转发到这里。
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path is not None else DEFAULT_HOLDINGS_PATH
        self._lock = threading.RLock()

    @property
    def path(self) -> Path:
        """供服务端做固定路径配置，不在异常消息中回显。"""

        return self._path

    def load(self) -> PortfolioSnapshot:
        """读取当前快照；文件不存在视为空仓，损坏文件则明确报错。"""

        with self._lock:
            if not self._path.exists():
                return PortfolioSnapshot()
            try:
                text = self._path.read_text(encoding="utf-8")
                raw = json.loads(text, parse_constant=_reject_json_constant)
            except (OSError, UnicodeError) as exc:
                raise PortfolioStorageError("持仓文件暂时无法读取") from exc
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise PortfolioCorruptionError("持仓文件损坏或格式无效，未自动清空") from exc
            if not isinstance(raw, Mapping):
                raise PortfolioCorruptionError("持仓文件损坏或格式无效，未自动清空")
            try:
                return PortfolioSnapshot.from_mapping(raw)
            except PortfolioValidationError as exc:
                raise PortfolioCorruptionError("持仓文件损坏或格式无效，未自动清空") from exc

    # ``read`` 是一个无副作用的同义入口，便于上层服务按语义命名。
    read = load

    def save(
        self,
        snapshot: PortfolioSnapshot | Mapping[str, object],
        *,
        expected_revision: int | None = None,
    ) -> PortfolioSnapshot:
        """以当前快照为基础替换全部持仓，并递增顶层 revision。

        传入 ``PortfolioSnapshot`` 且不显式给出 expected_revision 时，会使用
        该快照的 revision 进行乐观锁检查；传入 mapping 时建议显式给出版本。
        """

        candidate = _coerce_snapshot(snapshot)
        if expected_revision is None and isinstance(snapshot, PortfolioSnapshot):
            expected_revision = snapshot.revision
        return self.replace_holdings(candidate.holdings, expected_revision=expected_revision)

    save_snapshot = save

    def replace_holdings(
        self,
        holdings: Iterable[Holding | Mapping[str, object]],
        *,
        expected_revision: int | None = None,
    ) -> PortfolioSnapshot:
        records = tuple(_coerce_holding(item) for item in holdings)
        _validate_unique_holdings(records)

        def mutate(current: PortfolioSnapshot, next_revision: int) -> tuple[Holding, ...]:
            return tuple(
                Holding(
                    code=item.code,
                    name=item.name,
                    asset_type=item.asset_type,
                    quantity=item.quantity,
                    avg_cost_cny=item.avg_cost_cny,
                    acquired_date=item.acquired_date,
                    note=item.note,
                    revision=next_revision,
                    tactical_ratio=item.tactical_ratio,
                )
                for item in records
            )

        return self._mutate(expected_revision, mutate)

    def add_holding(
        self,
        holding: Holding | Mapping[str, object],
        *,
        expected_revision: int | None = None,
    ) -> PortfolioSnapshot:
        item = _coerce_holding(holding)

        def mutate(current: PortfolioSnapshot, next_revision: int) -> tuple[Holding, ...]:
            if _holding_key(item) in {_holding_key(existing) for existing in current.holdings}:
                raise _validation_error("持仓列表", "同一资产不能重复")
            added = Holding(
                code=item.code,
                name=item.name,
                asset_type=item.asset_type,
                quantity=item.quantity,
                avg_cost_cny=item.avg_cost_cny,
                acquired_date=item.acquired_date,
                note=item.note,
                revision=next_revision,
                tactical_ratio=item.tactical_ratio,
            )
            return (*current.holdings, added)

        return self._mutate(expected_revision, mutate)

    create_holding = add_holding
    create = add_holding

    def update_holding(
        self,
        code: str,
        changes: Holding | Mapping[str, object],
        *,
        asset_type: AssetType | None = None,
        expected_revision: int | None = None,
    ) -> PortfolioSnapshot:
        code = _validate_code(code)
        if asset_type is not None:
            asset_type = _validate_asset_type(asset_type)

        if isinstance(changes, Holding):
            patch: Mapping[str, object] = changes.to_dict()
        elif isinstance(changes, Mapping):
            _validate_mapping_fields(changes, _UPDATE_FIELDS, "持仓更新")
            patch = changes
        else:
            raise _validation_error("持仓更新", "必须是对象")

        def mutate(current: PortfolioSnapshot, next_revision: int) -> tuple[Holding, ...]:
            matches = [
                (index, existing)
                for index, existing in enumerate(current.holdings)
                if existing.code == code and (asset_type is None or existing.asset_type == asset_type)
            ]
            if not matches:
                raise _validation_error("持仓记录", "未找到指定资产")
            if len(matches) > 1:
                raise _validation_error("资产类型", "同一代码对应多个资产，请明确指定")
            index, existing = matches[0]
            merged = existing.to_dict()
            merged.update(patch)
            # The path parameter is the identity; allowing a different code here
            # would turn an update into an implicit delete+create operation.
            if merged.get("code") != existing.code:
                raise _validation_error("证券代码", "更新时不能改变代码")
            replacement = Holding.from_mapping({**merged, "revision": next_revision})
            result = list(current.holdings)
            result[index] = replacement
            _validate_unique_holdings(result)
            return tuple(result)

        return self._mutate(expected_revision, mutate)

    update = update_holding

    def delete_holding(
        self,
        code: str,
        *,
        asset_type: AssetType | None = None,
        expected_revision: int | None = None,
    ) -> PortfolioSnapshot:
        code = _validate_code(code)
        if asset_type is not None:
            asset_type = _validate_asset_type(asset_type)

        def mutate(current: PortfolioSnapshot, _next_revision: int) -> tuple[Holding, ...]:
            matches = [
                existing
                for existing in current.holdings
                if existing.code == code and (asset_type is None or existing.asset_type == asset_type)
            ]
            if not matches:
                raise _validation_error("持仓记录", "未找到指定资产")
            if len(matches) > 1:
                raise _validation_error("资产类型", "同一代码对应多个资产，请明确指定")
            return tuple(
                existing
                for existing in current.holdings
                if not (existing.code == code and (asset_type is None or existing.asset_type == asset_type))
            )

        return self._mutate(expected_revision, mutate)

    delete = delete_holding

    def get_holding(self, code: str, *, asset_type: AssetType | None = None) -> Holding | None:
        code = _validate_code(code)
        if asset_type is not None:
            asset_type = _validate_asset_type(asset_type)
        matches = [
            item
            for item in self.load().holdings
            if item.code == code and (asset_type is None or item.asset_type == asset_type)
        ]
        if len(matches) > 1:
            raise _validation_error("资产类型", "同一代码对应多个资产，请明确指定")
        return matches[0] if matches else None

    def list_holdings(self) -> tuple[Holding, ...]:
        return self.load().holdings

    def _mutate(
        self,
        expected_revision: int | None,
        mutator: Any,
    ) -> PortfolioSnapshot:
        if expected_revision is not None and (
            type(expected_revision) is not int or expected_revision < 0
        ):
            raise _validation_error("期望版本", "必须是非负整数")
        with self._lock:
            current = self.load()
            if expected_revision is not None and expected_revision != current.revision:
                raise PortfolioRevisionConflictError(expected_revision, current.revision)
            next_revision = current.revision + 1
            holdings = tuple(mutator(current, next_revision))
            _validate_unique_holdings(holdings)
            candidate = PortfolioSnapshot(
                schema_version=SCHEMA_VERSION,
                revision=next_revision,
                updated_at=datetime.now(UTC).isoformat(timespec="seconds"),
                holdings=holdings,
            )
            self._atomic_write(candidate)
            return candidate

    def _atomic_write(self, snapshot: PortfolioSnapshot) -> None:
        payload = json.dumps(
            snapshot.to_dict(),
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        )
        temporary_path: Path | None = None
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self._path.parent,
                prefix=f".{self._path.stem}-",
                suffix=".tmp",
                delete=False,
                newline="\n",
            ) as handle:
                temporary_path = Path(handle.name)
                handle.write(payload)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self._path)
            temporary_path = None
        except (OSError, UnicodeError, TypeError, ValueError) as exc:
            raise PortfolioStorageError("持仓文件无法安全保存，请检查本机权限或磁盘空间") from exc
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    # 原始保存错误更重要；绝不让清理错误覆盖它。
                    pass


class HoldingsStore(PortfolioStore):
    """PortfolioStore 的语义别名。"""


class PortfolioRepository(PortfolioStore):
    """PortfolioStore 的语义别名。"""


def _coerce_holding(value: Holding | Mapping[str, object]) -> Holding:
    if isinstance(value, Holding):
        return value
    if isinstance(value, Mapping):
        return Holding.from_mapping(value)
    raise _validation_error("持仓记录", "必须是对象")


def _coerce_snapshot(value: PortfolioSnapshot | Mapping[str, object]) -> PortfolioSnapshot:
    if isinstance(value, PortfolioSnapshot):
        return value
    if isinstance(value, Mapping):
        return PortfolioSnapshot.from_mapping(value)
    raise _validation_error("持仓快照", "必须是对象")


__all__ = [
    "DEFAULT_HOLDINGS_PATH",
    "SCHEMA_VERSION",
    "AssetType",
    "CorruptPortfolioError",
    "Holding",
    "HoldingsStore",
    "PortfolioCorruptionError",
    "PortfolioError",
    "PortfolioRepository",
    "PortfolioRevisionConflictError",
    "PortfolioSnapshot",
    "PortfolioStorageError",
    "PortfolioStore",
    "PortfolioValidationError",
    "RevisionConflictError",
]
