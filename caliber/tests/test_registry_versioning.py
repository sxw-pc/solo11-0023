from __future__ import annotations

from pathlib import Path

import pytest

from app.core.dsl_parser import parse_metric_file
from app.core.registry import MetricRegistry
from app.core.versioning import (
    diff_versions,
    get_latest,
    get_version,
    list_metrics,
    list_versions,
)
from app.errors import CaliberError, NotFoundError
from app.models import ImplementDef


def test_import_twice_creates_two_versions_and_history(
    conn, fixture_tree: Path, sql_roots: tuple[Path, ...]
):
    registry = MetricRegistry(conn, sql_roots)
    first = registry.import_directory(fixture_tree / "metrics")
    assert first.ok
    assert len(first.imported) == 4
    assert all(item.version == 1 for item in first.imported)

    second = registry.import_directory(fixture_tree / "metrics")
    assert second.ok
    assert all(item.version == 2 for item in second.imported)

    latest = get_latest(conn, "active_users")
    assert latest["version"] == 2
    assert latest["name"] == "活跃用户数"
    assert len(latest["definition"]["impls"]) == 2

    versions = list_versions(conn, "active_users")
    assert [item["version"] for item in versions] == [2, 1]
    assert get_version(conn, "active_users", 1)["definition"]["grain"] == "day"
    assert list_metrics(conn)[0]["key"] == "active_users"
    assert conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 8


def test_version_diff_reports_modified_field(
    conn, fixture_tree: Path, sql_roots: tuple[Path, ...]
):
    registry = MetricRegistry(conn, sql_roots)
    registry.import_file(fixture_tree / "metrics" / "active_users.yaml")
    metric = parse_metric_file(fixture_tree / "metrics" / "active_users.yaml")
    metric.name = "日活跃用户数（修订）"
    registry.import_definition(metric, "inline-test")

    diff = diff_versions(conn, "active_users", 1, 2)
    assert diff["changed"] is True
    modified = {item["field"]: item for item in diff["modified"]}
    assert modified["name"]["before"] == "活跃用户数"
    assert modified["name"]["after"] == "日活跃用户数（修订）"
    assert diff["added"] == []
    assert diff["removed"] == []


def test_version_diff_added_and_removed_fields(
    conn, fixture_tree: Path, sql_roots: tuple[Path, ...]
):
    registry = MetricRegistry(conn, sql_roots)
    metric = parse_metric_file(fixture_tree / "metrics" / "active_users.yaml")
    metric.impls.append(
        ImplementDef("impl_c", "users_a", "active_users_impl_a.sql")
    )
    registry.import_definition(metric)
    metric.impls.pop()
    registry.import_definition(metric)
    diff = diff_versions(conn, "active_users", 1, 2)
    removed_fields = {item["field"] for item in diff["removed"]}
    assert "impls[2].id" in removed_fields
    assert diff["modified"] == []


def test_missing_metric_raises_not_found(conn):
    with pytest.raises(NotFoundError):
        get_latest(conn, "not_exists")
    with pytest.raises(NotFoundError):
        get_version(conn, "not_exists", 1)


def test_import_missing_directory_raises(conn, sql_roots):
    with pytest.raises(CaliberError):
        MetricRegistry(conn, sql_roots).import_directory("does-not-exist")
