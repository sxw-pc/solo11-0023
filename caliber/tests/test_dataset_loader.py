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


def _write_csv(directory: Path, rel: str, header: str, *rows: str) -> None:
    path = directory / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join((header, *rows)) + "\n", encoding="utf-8")


def test_same_named_csvs_in_subdirs_get_distinct_stable_keys(tmp_path: Path):
    directory = tmp_path / "datasets"
    _write_csv(directory, "shared.csv", "id,v", "1,10", "2,20")
    _write_csv(directory, "region_a/shared.csv", "id,v", "3,30")
    _write_csv(directory, "region_b/nested/shared.csv", "id,v", "4,40", "5,50")
    catalog = DatasetCatalog(directory)
    try:
        metas = {item.key: item for item in catalog.list_metadata()}
        assert set(metas) == {
            "shared", "region_a__shared", "region_b__nested__shared"
        }
        assert metas["shared"].row_count == 2
        assert metas["region_a__shared"].row_count == 1
        assert metas["region_b__nested__shared"].row_count == 2
        assert metas["region_a__shared"].rel_path == "region_a/shared.csv"
        assert metas["region_b__nested__shared"].rel_path == (
            "region_b/nested/shared.csv"
        )
        # Every dataset has its own table; nothing was overwritten.
        readonly = catalog.readonly_connection()
        assert readonly.execute(
            'SELECT SUM(v) FROM "region_b__nested__shared"'
        ).fetchone()[0] == 90
        # Manifest order is deterministic across reloads.
        again = DatasetCatalog(directory)
        try:
            assert again.manifest() == catalog.manifest()
        finally:
            again.close()
    finally:
        catalog.close()


def test_bare_stem_ambiguity_is_reported_not_guessed(tmp_path: Path):
    directory = tmp_path / "datasets"
    _write_csv(directory, "x/shared.csv", "id,v", "1,10")
    _write_csv(directory, "y/shared.csv", "id,v", "2,20")
    catalog = DatasetCatalog(directory)
    try:
        assert catalog.resolve("shared") is None
        candidates = catalog.ambiguity("shared")
        assert {item.rel_path for item in candidates} == {
            "x/shared.csv", "y/shared.csv"
        }
        assert catalog.resolve("x__shared").rel_path == "x/shared.csv"
        assert catalog.resolve("missing") is None
    finally:
        catalog.close()


def test_manifest_references_carry_path_identity(catalog: DatasetCatalog):
    manifest = catalog.manifest()
    assert [item["key"] for item in manifest] == sorted(item["key"] for item in manifest)
    for ref in manifest:
        assert set(ref) == {"key", "name", "rel_path"}
        assert ref["rel_path"].endswith(".csv")
    users_a = next(item for item in manifest if item["key"] == "users_a")
    assert users_a["name"] == "users_a" and users_a["rel_path"] == "users_a.csv"


def test_key_collision_gets_deterministic_numeric_suffix(tmp_path: Path):
    # Header-style normalization makes the path-derived key of the nested
    # file collide with the top-level stem key; the suffix disambiguates.
    directory = tmp_path / "datasets"
    _write_csv(directory, "a__b.csv", "id,v", "1,1")
    _write_csv(directory, "a/b.csv", "id,v", "2,2")
    catalog = DatasetCatalog(directory)
    try:
        keys = {item.key for item in catalog.list_metadata()}
        assert keys == {"a__b", "a__b_2"}
        # Reloading assigns the same keys in the same order.
        again = DatasetCatalog(directory)
        try:
            assert [item.key for item in again.list_metadata()] == [
                item.key for item in catalog.list_metadata()
            ]
        finally:
            again.close()
    finally:
        catalog.close()

