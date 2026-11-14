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
        "orders_a", "orders_b", "users_a", "users_b",
        "revenue_a", "revenue_b",
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
