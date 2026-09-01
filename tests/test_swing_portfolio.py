from __future__ import annotations

import json
from pathlib import Path

import pytest

from trading_agent.swing.portfolio import (
    PortfolioCorruptionError,
    PortfolioRevisionConflictError,
    PortfolioStorageError,
    PortfolioStore,
    PortfolioValidationError,
)


def _holding(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "code": "999999",
        "name": "虚构资产",
        "asset_type": "etf",
        "quantity": 100,
        "avg_cost_cny": 10.5,
    }
    value.update(overrides)
    return value


def test_empty_store_has_no_file_and_roundtrip_survives_new_instance(tmp_path: Path) -> None:
    path = tmp_path / "private" / "holdings.json"
    first = PortfolioStore(path)

    empty = first.load()
    assert empty.revision == 0
    assert empty.holdings == ()
    assert not path.exists()

    saved = first.add_holding(_holding(), expected_revision=empty.revision)
    assert saved.revision == 1
    assert saved.holdings[0].tactical_ratio == 0.2
    assert saved.holdings[0].core_ratio == pytest.approx(0.8)

    restarted = PortfolioStore(path)
    loaded = restarted.load()
    assert loaded == saved
    assert json.loads(path.read_text(encoding="utf-8"))["schema_version"] == 1


def test_crud_and_revision_are_explicit(tmp_path: Path) -> None:
    store = PortfolioStore(tmp_path / "holdings.json")
    current = store.add_holding(_holding(), expected_revision=0)

    updated = store.update_holding(
        "999999",
        {"quantity": 125, "tactical_ratio": 0.35},
        expected_revision=current.revision,
    )
    assert updated.revision == 2
    assert updated.holdings[0].quantity == 125
    assert updated.holdings[0].tactical_ratio == pytest.approx(0.35)

    deleted = store.delete_holding("999999", expected_revision=updated.revision)
    assert deleted.revision == 3
    assert deleted.holdings == ()


def test_stale_revision_is_rejected_without_overwriting_newer_data(tmp_path: Path) -> None:
    path = tmp_path / "holdings.json"
    store = PortfolioStore(path)
    first = store.add_holding(_holding(), expected_revision=0)
    newer = store.update_holding(
        "999999", {"quantity": 110}, expected_revision=first.revision
    )
    before = path.read_bytes()

    with pytest.raises(PortfolioRevisionConflictError, match="刷新") as error:
        store.delete_holding("999999", expected_revision=first.revision)

    assert error.value.expected_revision == 1
    assert error.value.actual_revision == newer.revision
    assert path.read_bytes() == before
    assert store.load().holdings[0].quantity == 110


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("code", "99999", "6位"),
        ("asset_type", "fund", "资产类型"),
        ("quantity", 1.0, "正整数"),
        ("avg_cost_cny", float("nan"), "有限"),
        ("tactical_ratio", 0.51, "0到0.5"),
        ("note", "x" * 501, "备注"),
    ],
)
def test_holding_validation_rejects_bad_values(
    tmp_path: Path, field: str, value: object, message: str
) -> None:
    store = PortfolioStore(tmp_path / "holdings.json")
    with pytest.raises(PortfolioValidationError, match=message):
        store.add_holding(_holding(**{field: value}), expected_revision=0)
    assert not (tmp_path / "holdings.json").exists()


def test_unknown_fields_are_rejected_at_record_and_document_levels(tmp_path: Path) -> None:
    store = PortfolioStore(tmp_path / "holdings.json")

    with pytest.raises(PortfolioValidationError, match="不支持的字段"):
        store.add_holding(_holding(unexpected="do-not-store"), expected_revision=0)

    path = tmp_path / "holdings.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "revision": 0,
                "updated_at": "2026-01-01T00:00:00+00:00",
                "holdings": [],
                "unexpected": True,
            }
        ),
        encoding="utf-8",
    )
    before = path.read_bytes()
    with pytest.raises(PortfolioCorruptionError, match="未自动清空"):
        store.load()
    assert path.read_bytes() == before


def test_missing_required_fields_are_rejected_as_chinese_validation_errors(
    tmp_path: Path,
) -> None:
    store = PortfolioStore(tmp_path / "holdings.json")
    incomplete = _holding()
    del incomplete["avg_cost_cny"]

    with pytest.raises(PortfolioValidationError, match="缺少必要字段"):
        store.add_holding(incomplete, expected_revision=0)


def test_corrupt_file_is_not_silently_reset(tmp_path: Path) -> None:
    path = tmp_path / "holdings.json"
    path.write_text("{not-json", encoding="utf-8")
    before = path.read_bytes()

    with pytest.raises(PortfolioCorruptionError, match="损坏"):
        PortfolioStore(path).load()

    assert path.read_bytes() == before


def test_nan_in_persisted_json_is_rejected_and_preserved(tmp_path: Path) -> None:
    path = tmp_path / "holdings.json"
    path.write_text(
        '{"schema_version": 1, "revision": 1, '
        '"updated_at": "2026-01-01T00:00:00+00:00", "holdings": ['
        '{"code": "999999", "name": "虚构资产", "asset_type": "etf", '
        '"quantity": 100, "avg_cost_cny": NaN}]}' ,
        encoding="utf-8",
    )
    before = path.read_bytes()

    with pytest.raises(PortfolioCorruptionError, match="损坏"):
        PortfolioStore(path).load()
    assert path.read_bytes() == before


def test_atomic_replace_failure_keeps_old_file_and_cleans_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "holdings.json"
    store = PortfolioStore(path)
    original = store.add_holding(_holding(), expected_revision=0)
    before = path.read_bytes()

    def fail_replace(_source: object, _target: object) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr("trading_agent.swing.portfolio.os.replace", fail_replace)
    with pytest.raises(PortfolioStorageError, match="安全保存"):
        store.update_holding("999999", {"quantity": 101}, expected_revision=original.revision)

    assert path.read_bytes() == before
    assert PortfolioStore(path).load() == original
    assert list(tmp_path.glob(".holdings-*.tmp")) == []


def test_atomic_write_uses_private_directory_and_never_splits_sleeves(tmp_path: Path) -> None:
    path = tmp_path / "data" / "private" / "holdings.json"
    snapshot = PortfolioStore(path).add_holding(
        _holding(tactical_ratio=0.1), expected_revision=0
    )
    document = json.loads(path.read_text(encoding="utf-8"))
    record = document["holdings"][0]
    assert set(record) == {
        "code",
        "name",
        "asset_type",
        "quantity",
        "avg_cost_cny",
        "tactical_ratio",
        "revision",
    }
    assert "core_quantity" not in record
    assert "tactical_quantity" not in record
    assert snapshot.holdings[0].core_ratio == pytest.approx(0.9)


def test_future_acquired_date_is_rejected_without_writing(tmp_path: Path) -> None:
    store = PortfolioStore(tmp_path / "holdings.json")
    with pytest.raises(PortfolioValidationError, match="不能晚于今天"):
        store.add_holding(_holding(acquired_date="2999-01-01"), expected_revision=0)
    assert not (tmp_path / "holdings.json").exists()
