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


def test_one_broken_dataset_does_not_abort_the_batch(tmp_path: Path):
    directory = tmp_path / "mixed"
    directory.mkdir()
    (directory / "good.csv").write_text("id,v\n1,10\n2,20\n", encoding="utf-8")
    (directory / "broken.csv").write_text("a,b,c\n1,2\n", encoding="utf-8")
    catalog = DatasetCatalog(directory)
    try:
        # 正常数据集照常加载，列元信息与数据行对齐
        assert set(catalog.datasets) == {"good"}
        good = catalog.require("good")
        assert [(c.name, c.type) for c in good.columns] == [("id", "int"), ("v", "int")]
        rows = catalog.readonly_connection().execute("SELECT id, v FROM good").fetchall()
        assert [tuple(row) for row in rows] == [(1, 10), (2, 20)]
        # 失败被隔离且指明数据集/文件/原因
        assert len(catalog.load_errors) == 1
        error = catalog.load_errors[0]
        assert error["dataset"] == "broken"
        assert error["file"] == "broken.csv"
        assert "broken" in error["message"] and "列数" in error["message"]
    finally:
        catalog.close()


def test_all_datasets_broken_raises_with_details(tmp_path: Path):
    directory = tmp_path / "allbad"
    directory.mkdir()
    (directory / "broken.csv").write_text("a,b,c\n1,2\n", encoding="utf-8")
    with pytest.raises(DatasetError) as exc_info:
        DatasetCatalog(directory)
    assert exc_info.value.details and exc_info.value.details["errors"]


def test_bom_inside_header_and_all_empty_column(tmp_path: Path):
    directory = tmp_path / "bom"
    directory.mkdir()
    # 文件级 BOM + 列名内部夹带 BOM + 整列为空
    (directory / "sample.csv").write_bytes(
        "﻿order ﻿id,empty_note,score\n1,,9.5\n2,,7.0\n".encode("utf-8")
    )
    catalog = DatasetCatalog(directory)
    try:
        meta = catalog.require("sample")
        assert meta.encoding == "utf-8-sig"
        # BOM 被剥离，列名正常归一，不残留不可见字符
        assert [c.name for c in meta.columns] == ["order_id", "empty_note", "score"]
        # 整列空值也能推断出类型，不会崩
        types = {c.name: c.type for c in meta.columns}
        assert types["empty_note"] == "str"
        assert types["score"] == "float"
        rows = catalog.readonly_connection().execute(
            "SELECT order_id, empty_note, score FROM sample ORDER BY order_id"
        ).fetchall()
        assert [dict(row) for row in rows] == [
            {"order_id": 1, "empty_note": None, "score": 9.5},
            {"order_id": 2, "empty_note": None, "score": 7.0},
        ]
    finally:
        catalog.close()


def test_header_normalization_strips_embedded_bom():
    assert normalize_header("﻿amount ﻿usd", 0) == "amount_usd"


def test_duplicate_dataset_key_is_skipped_not_fatal(tmp_path: Path):
    # 两个不同文件名归一化为同一 key（大小写差异）
    collide = tmp_path / "collide"
    collide.mkdir()
    (collide / "data.csv").write_text("id,v\n1,10\n", encoding="utf-8")
    (collide / "Data.csv").write_text("id,v\n1,10\n", encoding="utf-8")
    if len(list(collide.glob("*.csv"))) != 2:
        pytest.skip("文件系统不区分大小写，无法构造同 key 冲突")
    catalog = DatasetCatalog(collide)
    try:
        assert "data" in catalog.datasets
        assert any(
            item["dataset"] == "data" and "重复" in item["message"]
            for item in catalog.load_errors
        )
    finally:
        catalog.close()


def test_duplicate_header_column_reports_dataset_and_column(tmp_path: Path):
    directory = tmp_path / "duphdr"
    directory.mkdir()
    (directory / "dup.csv").write_text("id,ID,v\n1,2,3\n", encoding="utf-8")
    (directory / "good.csv").write_text("id,v\n1,2\n", encoding="utf-8")
    catalog = DatasetCatalog(directory)
    try:
        assert set(catalog.datasets) == {"good"}
        error = next(item for item in catalog.load_errors if item["dataset"] == "dup")
        assert "dup.csv" in error["message"] and "id" in error["message"]
    finally:
        catalog.close()


def test_catalog_metadata_and_preview_still_aligned_after_skips(tmp_path: Path):
    directory = tmp_path / "aligned"
    directory.mkdir()
    (directory / "ok.csv").write_text("a,b\n1,x\n2,y\n", encoding="utf-8")
    (directory / "ragged.csv").write_text("a,b,c\n1,2\n", encoding="utf-8")
    catalog = DatasetCatalog(directory)
    try:
        preview = catalog.preview("ok")
        assert preview["meta"]["column_count"] == 2
        assert preview["rows"] == [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}]
    finally:
        catalog.close()


def test_type_inference():
    assert infer_type(["1", "2", "3"]) == "int"
    assert infer_type(["1", "2.5"]) == "float"
    assert infer_type(["2026-01-01", "2026-02-03"]) == "date"
    assert infer_type(["a", "1"]) == "str"
    assert infer_type(["", ""]) == "str"


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


def test_catalog_context_manager_closes_connection(tmp_path: Path):
    directory = tmp_path / "ctx"
    directory.mkdir()
    (directory / "ok.csv").write_text("id,v\n1,10\n", encoding="utf-8")
    with DatasetCatalog(directory) as catalog:
        assert catalog.require("ok").row_count == 1
    # close 后只读连接不应再可查询
    with pytest.raises(sqlite3.ProgrammingError):
        catalog.readonly_connection()


def test_missing_and_empty_directory_are_rejected(tmp_path: Path):
    with pytest.raises(DatasetError):
        DatasetCatalog(tmp_path / "missing")
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(DatasetError):
        DatasetCatalog(empty)


def test_persistence_failure_rolls_back_without_leaking_table(tmp_path: Path):
    # 超大整数在绑定阶段抛 OverflowError，验证保存点回滚与单文件隔离
    directory = tmp_path / "rollback"
    directory.mkdir()
    (directory / "good.csv").write_text("id,v\n1,10\n", encoding="utf-8")
    (directory / "boom.csv").write_text(f"id,v\n2,{2**64}\n", encoding="utf-8")
    catalog = DatasetCatalog(directory)
    try:
        assert set(catalog.datasets) == {"good"}
        error = next(item for item in catalog.load_errors if item["dataset"] == "boom")
        assert "落库失败" in error["message"]
        # 回滚后不存在残留的 boom 表，good 表仍可正常查询
        tables = {
            row[0]
            for row in catalog.readonly_connection().execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "boom" not in tables and "good" in tables
        assert catalog.readonly_connection().execute(
            "SELECT COUNT(*) FROM good"
        ).fetchone()[0] == 1
    finally:
        catalog.close()
