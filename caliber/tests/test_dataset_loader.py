from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.core.dataset_loader import (
    DatasetCatalog,
    infer_type,
    load_catalog,
    normalize_header,
)
from app.errors import DatasetError, NotFoundError


def test_fixture_catalog_metadata_and_types(catalog: DatasetCatalog):
    assert set(catalog.datasets) == {
        "orders_a", "orders_b", "users_a", "users_b"
    }
    users = catalog.require("users_a")
    assert users.row_count == 7
    assert users.column_count == 4
    types = {column.name: column.type for column in users.columns}
    assert types["user_id"] == "int"
    assert types["signup_date"] == "date"
    assert types["status"] == "str"
    orders = catalog.require("orders_a")
    assert {column.name: column.type for column in orders.columns}["amount"] == "float"


def test_preview_and_unknown_dataset(catalog: DatasetCatalog):
    payload = catalog.preview("orders_a", 2)
    assert payload["meta"]["key"] == "orders_a"
    assert len(payload["rows"]) == 2
    assert payload["rows"][0]["order_id"] == "A001"
    with pytest.raises(NotFoundError):
        catalog.require("unknown")


def test_utf8_sig_loading_and_header_normalization(tmp_path: Path):
    directory = tmp_path / "datasets"
    directory.mkdir()
    (directory / "daily.csv").write_bytes(
        "Order ID, Amount USD,event_date\n1, 12.5,2026-03-01\n2, 7.0,2026-03-02\n".encode(
            "utf-8-sig"
        )
    )
    catalog = DatasetCatalog(directory)
    try:
        meta = catalog.require("daily")
        assert meta.encoding == "utf-8-sig"
        assert [column.name for column in meta.columns] == [
            "order_id", "amount_usd", "event_date"
        ]
        assert [column.type for column in meta.columns] == ["int", "float", "date"]
        assert meta.row_count == 2
    finally:
        catalog.close()


@pytest.mark.parametrize(
    "content",
    [
        "",
        "user_id,user_name\n",
        "user_id,User ID\n1,a\n",
        "user_id,name\n1,a,extra\n",
    ],
)
def test_invalid_csv_is_rejected(tmp_path: Path, content: str):
    directory = tmp_path / "bad"
    directory.mkdir()
    (directory / "bad.csv").write_text(content, encoding="utf-8")
    with pytest.raises(DatasetError):
        DatasetCatalog(directory)


def test_type_inference():
    assert infer_type(["1", "2", "3"]) == "int"
    assert infer_type(["1", "2.5"]) == "float"
    assert infer_type(["2026-01-01", "2026-02-03"]) == "date"
    assert infer_type(["a", "1"]) == "str"
    assert infer_type(["", ""]) == "unknown"
    assert infer_type(["", "1", "", "2"]) == "int"


def test_header_normalization_rules():
    assert normalize_header(" Order ID ", 0) == "order_id"
    assert normalize_header("2nd Value", 1) == "col_2nd_value"
    with pytest.raises(DatasetError):
        normalize_header("   ", 0)


def test_readonly_connection_rejects_writes(catalog: DatasetCatalog):
    readonly = catalog.readonly_connection()
    assert readonly.execute("SELECT COUNT(*) FROM users_a").fetchone()[0] == 7
    with pytest.raises(sqlite3.OperationalError):
        readonly.execute("DELETE FROM users_a")


def _load(directory: Path):
    return DatasetCatalog(directory)


def test_gbk_file_is_decoded_with_fallback_metadata(tmp_path: Path):
    directory = tmp_path / "gbk"
    directory.mkdir()
    (directory / "sales.csv").write_bytes(
        "订单号,金额\nA001,12.5\nA002,7.0\n".encode("gbk")
    )
    catalog = _load(directory)
    try:
        meta = catalog.require("sales")
        assert meta.encoding == "gbk"
        assert meta.encoding_fallback is True
        assert meta.bom is False
        assert meta.row_count == 2
        assert [column.original_name for column in meta.columns] == ["订单号", "金额"]
        assert [column.type for column in meta.columns] == ["str", "float"]
        chain = meta.load_metadata["encoding"]["candidates"]
        assert [item["encoding"] for item in chain] == ["utf-8", "gbk"]
        assert [item["ok"] for item in chain] == [False, True]
        assert chain[0]["line"] == 1
        assert "UTF-8" in meta.load_metadata["warnings"][0]
    finally:
        catalog.close()


def test_utf8_bom_file_keeps_encoding_marker_and_clean_column_name(tmp_path: Path):
    directory = tmp_path / "bom"
    directory.mkdir()
    (directory / "events.csv").write_bytes(
        "Order ID,Amount\n1,3.5\n2,4.5\n".encode("utf-8-sig")
    )
    catalog = _load(directory)
    try:
        meta = catalog.require("events")
        assert meta.encoding == "utf-8-sig"
        assert meta.encoding_fallback is False
        assert meta.bom is True
        assert [column.name for column in meta.columns] == ["order_id", "amount"]
        assert meta.load_metadata["type_inference"]["unknown_columns"] == []
    finally:
        catalog.close()


def test_all_empty_column_becomes_unknown(tmp_path: Path):
    directory = tmp_path / "empty"
    directory.mkdir()
    (directory / "holes.csv").write_text(
        "id,note,amount\n1,,2.0\n2,,3.0\n", encoding="utf-8"
    )
    catalog = _load(directory)
    try:
        meta = catalog.require("holes")
        types = {column.name: column.type for column in meta.columns}
        assert types == {"id": "int", "note": "unknown", "amount": "float"}
        assert meta.load_metadata["type_inference"]["unknown_columns"] == ["note"]
        assert any("unknown" in item for item in meta.load_metadata["warnings"])
    finally:
        catalog.close()


def test_late_numeric_values_infer_numeric_type(tmp_path: Path):
    directory = tmp_path / "late"
    directory.mkdir()
    (directory / "payments.csv").write_text(
        "id,amount\n1,\n2,\n3,10\n4,20\n", encoding="utf-8"
    )
    catalog = _load(directory)
    try:
        meta = catalog.require("payments")
        assert {column.name: column.type for column in meta.columns}["amount"] == "int"
        assert meta.load_metadata["type_inference"]["empty_cell_count"] == 2
        preview = catalog.preview("payments", 4)["rows"]
        assert preview[0]["amount"] is None
        assert preview[2]["amount"] == 10
        # Numeric SQL must agree with the inferred type.
        total = catalog.readonly_connection().execute(
            "SELECT SUM(amount) FROM payments"
        ).fetchone()[0]
        assert total == 30
    finally:
        catalog.close()


def test_metadata_is_stable_across_repeated_loads(tmp_path: Path):
    directory = tmp_path / "stable"
    directory.mkdir()
    (directory / "table.csv").write_bytes(
        "id,amount\n1,\n2,2.5\n".encode("gbk")
    )
    first = _load(directory)
    try:
        first_payload = [item.to_dict() for item in first.list_metadata()]
    finally:
        first.close()
    second = _load(directory)
    try:
        second_payload = [item.to_dict() for item in second.list_metadata()]
    finally:
        second.close()
    assert first_payload == second_payload
    cache = first_payload[0]["load_metadata"]["cache"]
    assert cache["compute_once_per_load"] is True
    assert cache["reuse_across_loads"] is False


def test_undecodable_file_is_isolated_and_located(tmp_path: Path):
    directory = tmp_path / "mixed"
    directory.mkdir()
    (directory / "good.csv").write_text("id\n1\n2\n", encoding="utf-8")
    (directory / "broken.csv").write_bytes(b"\x00\x01\x02binary\n")
    catalog = _load(directory)
    try:
        assert set(catalog.datasets) == {"good"}
        assert catalog.require("good").row_count == 2
        assert [item["file"] for item in catalog.errors] == [
            str((directory / "broken.csv").resolve())
        ]
        record = catalog.errors[0]
        assert record["details"]["reason"] == "binary_content"
        assert record["details"]["line"] == 1
        with pytest.raises(DatasetError) as excinfo:
            catalog.require("broken")
        assert "broken.csv" in excinfo.value.message
        assert "broken" in excinfo.value.details["message"]
    finally:
        catalog.close()


def test_same_stem_in_one_directory_does_not_overwrite_metadata(tmp_path: Path):
    directory = tmp_path / "collision"
    directory.mkdir()
    # Both names normalize to the dataset key "data_one"; the second must not
    # overwrite the first file's metadata even though its encoding differs.
    (directory / "data one.csv").write_text("id\n1\n", encoding="utf-8")
    (directory / "data-one.csv").write_bytes("id,name\n2,甲\n".encode("gbk"))
    catalog = _load(directory)
    try:
        assert set(catalog.datasets) == {"data_one"}
        meta = catalog.require("data_one")
        assert meta.encoding == "utf-8"
        assert meta.row_count == 1
        assert [item["details"]["reason"] for item in catalog.errors] == [
            "duplicate_key"
        ]
        assert catalog.errors[0]["details"]["existing_file"].endswith("data one.csv")
    finally:
        catalog.close()


def test_failed_file_does_not_leave_partial_metadata_or_table(tmp_path: Path):
    directory = tmp_path / "partial"
    directory.mkdir()
    (directory / "same.csv").write_text("id\n1\n", encoding="utf-8")
    (directory / "bad.csv").write_bytes(b"\x00")
    catalog = _load(directory)
    try:
        assert set(catalog.datasets) == {"same"}
        assert catalog.errors[0]["key"] == "bad"
        assert catalog.require("same").encoding == "utf-8"
        tables = {
            row[0]
            for row in catalog._conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert tables == {"same"}
    finally:
        catalog.close()


def test_empty_directory_of_unreadable_files_raises_aggregate(tmp_path: Path):
    directory = tmp_path / "allbad"
    directory.mkdir()
    (directory / "a.csv").write_bytes(b"\x00")
    (directory / "b.csv").write_bytes(b"\x01")
    with pytest.raises(DatasetError) as excinfo:
        _load(directory)
    details = excinfo.value.details
    assert len(details["files"]) == 2
    assert {item["file"] for item in details["files"]} == {
        str((directory / "a.csv").resolve()),
        str((directory / "b.csv").resolve()),
    }


def test_header_only_file_reports_file_and_reason(tmp_path: Path):
    directory = tmp_path / "headeronly"
    directory.mkdir()
    (directory / "nothing.csv").write_text("id\n", encoding="utf-8")
    with pytest.raises(DatasetError) as excinfo:
        _load(directory)
    assert "nothing.csv" in excinfo.value.message
    assert excinfo.value.details["files"][0]["details"]["reason"] == "no_data_rows"


def test_file_replaced_during_read_is_retried_then_flagged(tmp_path: Path, monkeypatch):
    directory = tmp_path / "changing"
    directory.mkdir()
    (directory / "hot.csv").write_text("id,amount\n1,2\n", encoding="utf-8")

    from app.core import dataset_loader

    real_signature = dataset_loader._signature
    counter = {"value": 0}

    def first_read_mismatch(stat_result):
        counter["value"] += 1
        base = real_signature(stat_result)
        if counter["value"] == 1:
            # Simulate the path being swapped between open() and stat().
            return (base[0], base[1], base[2] + 1, base[3])
        return base

    monkeypatch.setattr(dataset_loader, "_signature", first_read_mismatch)
    catalog = _load(directory)
    try:
        meta = catalog.require("hot")
        assert meta.row_count == 1
        assert meta.load_metadata["source"]["read_attempts"] == 2
        assert meta.load_metadata["source"]["stable"] is True
    finally:
        catalog.close()


def test_unstable_file_fails_with_explicit_reason(tmp_path: Path, monkeypatch):
    directory = tmp_path / "unstable"
    directory.mkdir()
    target = directory / "unstable.csv"
    target.write_text("id,amount\n1,2\n", encoding="utf-8")

    from app.core import dataset_loader

    real_signature = dataset_loader._signature
    counter = {"value": 0}

    def drifting_signature(stat_result):
        counter["value"] += 1
        base = real_signature(stat_result)
        return base if counter["value"] % 2 else (base[0], base[1], base[2] + 1, base[3])

    monkeypatch.setattr(dataset_loader, "_signature", drifting_signature)
    with pytest.raises(DatasetError) as excinfo:
        _load(directory)
    record = excinfo.value.details["files"][0]
    assert record["details"]["reason"] == "file_changed_during_read"
    assert record["details"]["attempts"] == 2
    assert "unstable.csv" in record["message"]
