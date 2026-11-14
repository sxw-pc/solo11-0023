from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from app.core.dsl_parser import parse_metric_file
from app.core.registry import MetricRegistry
from app.core.dataset_loader import DatasetCatalog
from app.core.snapshot import perform_run, snapshots_for_run
from app.core.versioning import (
    diff_versions,
    get_latest,
    get_version,
    impls_for_version,
    list_metrics,
    list_versions,
)
from app.errors import CaliberError, NotFoundError
from app.models import ImplementDef


def test_unchanged_reimport_keeps_current_version(
    conn, fixture_tree: Path, sql_roots: tuple[Path, ...]
):
    registry = MetricRegistry(conn, sql_roots)
    first = registry.import_directory(fixture_tree / "metrics")
    assert first.ok
    assert len(first.imported) == 3
    assert all(item.version == 1 for item in first.imported)

    for _ in range(2):
        repeat = registry.import_directory(fixture_tree / "metrics")
        assert repeat.ok
        assert all(item.version == 1 for item in repeat.imported)

    latest = get_latest(conn, "active_users")
    assert latest["version"] == 1
    assert latest["name"] == "活跃用户数"
    assert len(latest["definition"]["impls"]) == 2

    versions = list_versions(conn, "active_users")
    assert [item["version"] for item in versions] == [1]
    assert get_version(conn, "active_users", 1)["definition"]["grain"] == "day"
    assert conn.execute("SELECT COUNT(*) FROM metric_versions").fetchone()[0] == 3
    assert list_metrics(conn)[0]["key"] == "active_users"
    assert conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 9


def test_raw_yaml_rewrite_without_semantic_change_stays_same_version(
    conn, fixture_tree: Path, sql_roots: tuple[Path, ...]
):
    registry = MetricRegistry(conn, sql_roots)
    path = fixture_tree / "metrics" / "active_users.yaml"
    registry.import_file(path)

    # Comments, field order and whitespace change the raw bytes but not the
    # parsed definition, so no new version may be created.
    original = parse_metric_file(path)
    reordered = dict(reversed(list(original.to_dict().items())))
    path.write_text(
        "# rewritten fixture\n"
        + yaml.safe_dump(reordered, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    result = registry.import_file(path)
    assert result.version == 1
    assert len(list_versions(conn, "active_users")) == 1

    # A real definition change still advances the version.
    changed = parse_metric_file(path)
    changed.name = "活跃用户数（新口径）"
    assert registry.import_definition(changed, "inline-test") == 2
    assert get_latest(conn, "active_users")["version"] == 2


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


def test_run_only_executes_current_version_impls(
    conn, fixture_tree: Path, sql_roots: tuple[Path, ...]
):
    registry = MetricRegistry(conn, sql_roots)

    # v1 declares a third implementation that is later retired in v2.
    (fixture_tree / "impls" / "active_users_impl_c.sql").write_text(
        "SELECT last_active_date AS day, COUNT(DISTINCT user_id) AS value "
        "FROM users_a WHERE status = 'active' GROUP BY last_active_date",
        encoding="utf-8",
    )
    retired = ImplementDef("impl_retired", "users_a", "active_users_impl_c.sql")
    grown = parse_metric_file(fixture_tree / "metrics" / "active_users.yaml")
    grown.impls.append(retired)
    assert registry.import_definition(grown) == 1

    shrunk = parse_metric_file(fixture_tree / "metrics" / "active_users.yaml")
    assert registry.import_definition(shrunk) == 2

    catalog = DatasetCatalog(fixture_tree / "datasets")
    try:
        run_id = perform_run(
            conn, ["active_users"], catalog, sql_roots,
            datasets_dir=fixture_tree / "datasets", timeout_seconds=2,
            max_rows=100,
        )
    finally:
        catalog.close()
    (snapshot,) = snapshots_for_run(conn, run_id)
    # Only the current version's declared implementations participate.
    assert snapshot["metric_version"] == 2
    assert snapshot["impl_ids"] == ["impl_a", "impl_b"]
    assert snapshot["summary"]["impl_count"] == 2
    assert {item["impl_id"] for item in snapshot["result"]["implementations"]} == {
        "impl_a", "impl_b",
    }


def test_version_changes_keep_impls_scoped_per_version(
    conn, fixture_tree: Path, sql_roots: tuple[Path, ...]
):
    registry = MetricRegistry(conn, sql_roots)
    original = parse_metric_file(fixture_tree / "metrics" / "active_users.yaml")
    registry.import_definition(original)  # v1: impl_a, impl_b

    grown = parse_metric_file(fixture_tree / "metrics" / "active_users.yaml")
    grown.impls.append(
        ImplementDef("impl_c", "users_a", "active_users_impl_a.sql")
    )
    registry.import_definition(grown)  # v2: impl_a, impl_b, impl_c

    shrunk = parse_metric_file(fixture_tree / "metrics" / "active_users.yaml")
    registry.import_definition(shrunk)  # v3: impl_a, impl_b again

    assert [
        item.id for item in impls_for_version(conn, "active_users", 1)
    ] == ["impl_a", "impl_b"]
    assert [
        item.id for item in impls_for_version(conn, "active_users", 2)
    ] == ["impl_a", "impl_b", "impl_c"]
    # The retired impl belongs to v2 only; v3 does not inherit it.
    assert [
        item.id for item in impls_for_version(conn, "active_users", 3)
    ] == ["impl_a", "impl_b"]
    # Each version keeps its own list: shrinking never rewrites an older one.
    assert conn.execute(
        "SELECT COUNT(*) FROM metric_impls WHERE metric_key = ? AND version = 2",
        ("active_users",),
    ).fetchone()[0] == 3


def test_legacy_repository_backfills_impls_without_reimport(
    conn, fixture_tree: Path, sql_roots: tuple[Path, ...]
):
    """A repository written before metric_impls existed keeps working."""
    registry = MetricRegistry(conn, sql_roots)
    metric = parse_metric_file(fixture_tree / "metrics" / "active_users.yaml")
    registry.import_definition(metric)

    # Simulate an old artifact: version rows exist, per-version impl rows do not.
    conn.execute("DELETE FROM metric_impls")
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM metric_impls").fetchone()[0] == 0

    legacy = impls_for_version(conn, "active_users", 1)
    assert [item.id for item in legacy] == ["impl_a", "impl_b"]

    changed = parse_metric_file(fixture_tree / "metrics" / "active_users.yaml")
    changed.impls = changed.impls[:1]
    changed.impls.append(ImplementDef("impl_c", "users_a", "active_users_impl_a.sql"))
    assert registry.import_definition(changed) == 2
    # Old version was materialized from its own immutable definition, not from
    # the newly imported one; the new version has only what it declares.
    assert [item.id for item in impls_for_version(conn, "active_users", 1)] == [
        "impl_a", "impl_b",
    ]
    assert [item.id for item in impls_for_version(conn, "active_users", 2)] == [
        "impl_a", "impl_c",
    ]


def test_failed_import_reports_key_version_and_impl_and_keeps_current(
    conn, fixture_tree: Path, sql_roots: tuple[Path, ...]
):
    registry = MetricRegistry(conn, sql_roots)
    first = registry.import_directory(fixture_tree / "metrics")
    assert first.ok

    path = fixture_tree / "metrics" / "conversion_rate.yaml"
    broken = path.read_text(encoding="utf-8").replace(
        "conversion_rate_impl_a.sql", "../../etc/passwd.sql"
    )
    path.write_text(broken, encoding="utf-8")

    summary = registry.import_directory(fixture_tree / "metrics")
    assert summary.ok is False
    assert len(summary.imported) == 2  # other files still import
    (error,) = summary.errors
    assert error["file"] == str(path)
    assert error["metric_key"] == "conversion_rate"
    assert error["impl_ids"] == ["impl_a"]
    assert error["version"] == 2  # candidate that was never written
    assert error["current_version"] == 1
    assert "impls[0].sql_file" in error["fields"]

    # The failure did not pollute the existing version pointer or history.
    assert get_latest(conn, "conversion_rate")["version"] == 1
    assert len(list_versions(conn, "conversion_rate")) == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM metric_versions WHERE metric_key = ?",
        ("conversion_rate",),
    ).fetchone()[0] == 1
    audit = conn.execute(
        "SELECT detail_json FROM audit_log WHERE entity = ? AND status = 'error'",
        (str(path),),
    ).fetchone()
    assert audit is not None
    detail = json.loads(audit["detail_json"])
    assert detail["metric_key"] == "conversion_rate"
    assert detail["impl_ids"] == ["impl_a"]
    assert detail["attempted_version"] == 2
    assert detail["current_version"] == 1


def test_import_failure_before_key_parse_reports_null_key(
    conn, fixture_tree: Path, sql_roots: tuple[Path, ...]
):
    path = fixture_tree / "metrics" / "broken.yaml"
    path.write_text("key: [unclosed", encoding="utf-8")
    summary = MetricRegistry(conn, sql_roots).import_directory(
        fixture_tree / "metrics"
    )
    broken = next(item for item in summary.errors if item["file"] == str(path))
    assert broken["metric_key"] is None
    assert broken["version"] is None
    assert broken["impl_ids"] == []


def test_missing_metric_raises_not_found(conn):
    with pytest.raises(NotFoundError):
        get_latest(conn, "not_exists")
    with pytest.raises(NotFoundError):
        get_version(conn, "not_exists", 1)


def test_import_missing_directory_raises(conn, sql_roots):
    with pytest.raises(CaliberError):
        MetricRegistry(conn, sql_roots).import_directory("does-not-exist")
