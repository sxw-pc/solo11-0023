from __future__ import annotations

from pathlib import Path

import pytest

from app.core.dsl_validator import assert_valid, resolve_sql_path, validate_definition
from app.errors import NotFoundError, UnsafeSQLError, ValidationError
from app.models import ImplementDef, MetricDefinition


@pytest.fixture
def sql_dir(tmp_path: Path) -> Path:
    root = tmp_path / "impls"
    root.mkdir()
    (root / "a.sql").write_text("SELECT 1;", encoding="utf-8")
    (root / "b.sql").write_text("SELECT 2;", encoding="utf-8")
    return root


def make_metric(**overrides) -> MetricDefinition:
    values = {
        "key": "daily_orders",
        "name": "每日订单数",
        "owner": "data-platform",
        "description": "测试指标",
        "unit": "单",
        "grain": "day",
        "impls": [
            ImplementDef("impl_a", "orders_a", "a.sql"),
            ImplementDef("impl_b", "orders_b", "b.sql"),
        ],
    }
    values.update(overrides)
    return MetricDefinition(**values)


def test_valid_definition_has_no_issues(sql_dir: Path):
    assert validate_definition(make_metric(), (sql_dir,)) == []
    assert_valid(make_metric(), (sql_dir,))


def test_invalid_key_and_grain_are_structured(sql_dir: Path):
    issues = validate_definition(
        make_metric(key="Bad-Key", grain="year"), (sql_dir,)
    )
    by_field = {item.field: item for item in issues}
    assert by_field["key"].code == "pattern"
    assert by_field["grain"].code == "enum"


def test_requires_at_least_two_implementations(sql_dir: Path):
    issues = validate_definition(
        make_metric(impls=[ImplementDef("impl_a", "orders_a", "a.sql")]),
        (sql_dir,),
    )
    assert any(item.field == "impls" and item.code == "min_items" for item in issues)


def test_duplicate_impl_id_is_rejected(sql_dir: Path):
    issues = validate_definition(
        make_metric(
            impls=[
                ImplementDef("impl_a", "orders_a", "a.sql"),
                ImplementDef("impl_a", "orders_b", "b.sql"),
            ]
        ),
        (sql_dir,),
    )
    assert any(item.code == "duplicate" for item in issues)


@pytest.mark.parametrize("path", ["../secret.sql", "../../etc/passwd.sql", "/tmp/a.sql"])
def test_path_traversal_and_absolute_paths_are_rejected(sql_dir: Path, path: str):
    issues = validate_definition(
        make_metric(
            impls=[
                ImplementDef("impl_a", "orders_a", path),
                ImplementDef("impl_b", "orders_b", "b.sql"),
            ]
        ),
        (sql_dir,),
    )
    assert any(item.code == "unsafe_path" for item in issues)


def test_missing_sql_file_is_rejected(sql_dir: Path):
    issues = validate_definition(
        make_metric(
            impls=[
                ImplementDef("impl_a", "orders_a", "missing.sql"),
                ImplementDef("impl_b", "orders_b", "b.sql"),
            ]
        ),
        (sql_dir,),
    )
    assert any(item.code == "missing_file" for item in issues)


def test_no_sql_roots_and_bad_impl_id(sql_dir: Path):
    issues = validate_definition(
        make_metric(
            impls=[
                ImplementDef("UPPER", "orders_a", "a.sql"),
                ImplementDef("impl_b", "orders_b", "b.sql"),
            ]
        ),
        (),
    )
    fields = {item.field for item in issues}
    assert "impls" in fields
    assert "impls[0].id" in fields


def test_assert_valid_raises_validation_error(sql_dir: Path):
    with pytest.raises(ValidationError) as caught:
        assert_valid(make_metric(grain="hour"), (sql_dir,))
    assert caught.value.issues[0]["field"] == "grain"
    assert caught.value.http_status == 400


def test_resolve_sql_path_inside_root_and_subdirectory(sql_dir: Path):
    resolved = resolve_sql_path("a.sql", [sql_dir])
    assert resolved.name == "a.sql"
    sub = sql_dir / "nested"
    sub.mkdir()
    (sub / "c.sql").write_text("SELECT 3;", encoding="utf-8")
    assert resolve_sql_path("nested/c.sql", [sql_dir]).parent == sub


def test_resolve_missing_and_unsafe(sql_dir: Path):
    with pytest.raises(NotFoundError):
        resolve_sql_path("none.sql", [sql_dir])
    with pytest.raises(UnsafeSQLError):
        resolve_sql_path("../a.sql", [sql_dir])
