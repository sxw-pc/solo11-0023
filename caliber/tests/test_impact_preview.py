"""Tests for the pre-publish impact preview and strict/lenient import modes."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.dsl_parser import parse_metric_file
from app.core.impact import (
    RISK_IMPL_SHRINK,
    RISK_SQL_FINGERPRINT,
    build_impact_preview,
    sql_fingerprint,
)
from app.core.registry import MetricRegistry
from app.core.versioning import get_latest, list_versions
from app.db import table_counts
from app.errors import ImpactBlockedError
from app.models import ImplementDef, MetricDefinition


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _write_metric(directory: Path, metric: MetricDefinition) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{metric.key}.yaml"
    lines = [
        f"key: {metric.key}",
        f"name: {metric.name}",
        f"owner: {metric.owner}",
        f"description: {metric.description}",
        f"unit: {metric.unit}",
        f"grain: {metric.grain}",
        "impls:",
    ]
    for impl in metric.impls:
        lines.append(f"  - id: {impl.id}")
        lines.append(f"    source: {impl.source}")
        lines.append(f"    sql_file: {impl.sql_file}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _active_users(fixture_tree: Path) -> MetricDefinition:
    return parse_metric_file(fixture_tree / "metrics" / "active_users.yaml")


def _history(conn, key: str, run_ids: list[int], snapshot_count: int) -> None:
    """Assert the preview's affected-history query matches the real tables."""
    rows = conn.execute(
        "SELECT DISTINCT run_id FROM snapshots WHERE metric_key = ? ORDER BY run_id",
        (key,),
    ).fetchall()
    assert [row[0] for row in rows] == run_ids
    assert conn.execute(
        "SELECT COUNT(*) FROM snapshots WHERE metric_key = ?", (key,)
    ).fetchone()[0] == snapshot_count


# Tables that must never change on a blocked/preview action. audit_log is
# excluded on purpose: strict blocking is required to write an audit row.
REGISTRY_TABLES = ("metrics", "metric_versions", "runs", "snapshots")


def _registry_counts(conn) -> dict[str, int]:
    return {name: table_counts(conn)[name] for name in REGISTRY_TABLES}


# ---------------------------------------------------------------------------
# Fingerprint primitives
# ---------------------------------------------------------------------------
def test_sql_fingerprint_detects_content_change_not_just_filename(
    fixture_tree: Path, sql_roots
):
    sql_path = fixture_tree / "impls" / "active_users_impl_a.sql"
    original = sql_path.read_text(encoding="utf-8")
    fp_before, warn = sql_fingerprint("active_users_impl_a.sql", sql_roots)
    assert warn is None and fp_before and fp_before.startswith("sha256:")

    # Same file name, different bytes -> fingerprint must change.
    sql_path.write_text(original + "\n-- extra comment line\n", encoding="utf-8")
    fp_after, _ = sql_fingerprint("active_users_impl_a.sql", sql_roots)
    assert fp_after != fp_before

    # Restore -> fingerprint returns to the original (deterministic).
    sql_path.write_text(original, encoding="utf-8")
    fp_restored, _ = sql_fingerprint("active_users_impl_a.sql", sql_roots)
    assert fp_restored == fp_before


def test_sql_fingerprint_missing_file_degrades_without_raising(sql_roots):
    fp, warn = sql_fingerprint("does_not_exist.sql", sql_roots)
    assert fp is None
    assert warn and "指纹" in warn


# ---------------------------------------------------------------------------
# Brand-new metric
# ---------------------------------------------------------------------------
def test_preview_brand_new_metric_is_not_high_risk(conn, fixture_tree, sql_roots):
    metric = _active_users(fixture_tree)
    preview = build_impact_preview(conn, metric, sql_roots, source_file="x.yaml")
    data = preview.to_dict()
    assert data["exists"] is False
    assert data["current_version"] is None
    assert data["candidate_version"] == 1
    assert data["high_risk"] is False
    assert data["added_impls"] == ["impl_a", "impl_b"]
    assert data["removed_impls"] == []
    assert data["affected_run_ids"] == []
    assert data["affected_snapshot_count"] == 0


# ---------------------------------------------------------------------------
# Scenario 1: append an impl
# ---------------------------------------------------------------------------
def test_preview_append_impl_lists_added_and_affected_runs(
    registry_conn, fixture_tree, sql_roots, execute_fixture_run
):
    run_1 = execute_fixture_run(["active_users"])
    run_2 = execute_fixture_run(["active_users"])

    metric = _active_users(fixture_tree)
    metric.impls.append(
        ImplementDef("impl_c", "users_a", "active_users_impl_a.sql")
    )
    preview = build_impact_preview(
        registry_conn, metric, sql_roots, source_file="active_users.yaml"
    ).to_dict()

    assert preview["exists"] is True
    assert preview["current_version"] == 1
    assert preview["candidate_version"] == 2
    assert preview["added_impls"] == ["impl_c"]
    assert preview["removed_impls"] == []
    assert preview["high_risk"] is False
    assert preview["risk_reasons"] == []
    assert preview["affected_run_ids"] == [run_1, run_2]
    assert preview["affected_snapshot_count"] == 2
    _history(registry_conn, "active_users", [run_1, run_2], 2)


# ---------------------------------------------------------------------------
# Scenario 2: shrink impls (high risk)
# ---------------------------------------------------------------------------
def test_preview_shrink_impls_is_high_risk(
    registry_conn, fixture_tree, sql_roots, execute_fixture_run
):
    run_id = execute_fixture_run(["active_users"])
    metric = _active_users(fixture_tree)
    metric.impls = [metric.impls[0]]  # keep impl_a only -> below min count, preview still works

    preview = build_impact_preview(
        registry_conn, metric, sql_roots
    ).to_dict()
    assert preview["removed_impls"] == ["impl_b"]
    assert preview["added_impls"] == []
    assert preview["high_risk"] is True
    assert RISK_IMPL_SHRINK in preview["risk_reasons"]
    assert preview["affected_run_ids"] == [run_id]
    assert preview["affected_snapshot_count"] == 1


# ---------------------------------------------------------------------------
# Scenario 3: SQL content-only change (high risk, same file name)
# ---------------------------------------------------------------------------
def test_preview_sql_content_change_is_high_risk(
    registry_conn, fixture_tree, sql_roots, execute_fixture_run
):
    run_id = execute_fixture_run(["active_users"])
    sql_path = fixture_tree / "impls" / "active_users_impl_a.sql"
    original = sql_path.read_text(encoding="utf-8")
    try:
        sql_path.write_text(original + "\n-- changed body, same filename\n", encoding="utf-8")
        metric = _active_users(fixture_tree)
        preview = build_impact_preview(
            registry_conn, metric, sql_roots
        ).to_dict()
    finally:
        sql_path.write_text(original, encoding="utf-8")

    assert preview["added_impls"] == [] and preview["removed_impls"] == []
    assert preview["high_risk"] is True
    assert RISK_SQL_FINGERPRINT in preview["risk_reasons"]
    changes = preview["sql_fingerprint_changes"]
    assert len(changes) == 1
    change = changes[0]
    assert change["impl_id"] == "impl_a"
    assert change["before_fingerprint"] != change["after_fingerprint"]
    modified = {item["impl_id"]: item for item in preview["modified_impls"]}
    assert modified["impl_a"]["sql_fingerprint_changed"] is True
    assert preview["affected_run_ids"] == [run_id]


def test_preview_unchanged_sql_is_stable_and_not_high_risk(
    registry_conn, fixture_tree, sql_roots
):
    metric = _active_users(fixture_tree)
    first = build_impact_preview(registry_conn, metric, sql_roots).to_dict()
    second = build_impact_preview(registry_conn, metric, sql_roots).to_dict()
    # Re-importing the identical file yields an identical verdict and no risk.
    assert first == second
    assert first["changed"] is False
    assert first["high_risk"] is False
    assert first["sql_fingerprint_changes"] == []


# ---------------------------------------------------------------------------
# grain / unit changes
# ---------------------------------------------------------------------------
def test_preview_reports_grain_and_unit_changes(
    registry_conn, fixture_tree, sql_roots
):
    metric = _active_users(fixture_tree)
    metric.grain = "week"
    metric.unit = "千人"
    preview = build_impact_preview(
        registry_conn, metric, sql_roots
    ).to_dict()
    assert preview["grain_changed"] is True
    assert preview["grain_before"] == "day"
    assert preview["grain_after"] == "week"
    assert preview["unit_changed"] is True
    assert preview["unit_before"] == "人"
    assert preview["unit_after"] == "千人"
    # grain/unit change alone is not in the high-risk rule set.
    assert preview["high_risk"] is False
    assert preview["changed"] is True


# ---------------------------------------------------------------------------
# Strict mode: high risk blocked, database untouched, audit written
# ---------------------------------------------------------------------------
def test_strict_mode_blocks_shrink_and_does_not_write(
    registry_conn, fixture_tree, sql_roots, execute_fixture_run
):
    execute_fixture_run(["active_users"])
    before = _registry_counts(registry_conn)
    metric = _active_users(fixture_tree)
    metric.impls = [metric.impls[0]]

    strict = MetricRegistry(registry_conn, sql_roots, strict=True)
    with pytest.raises(ImpactBlockedError) as excinfo:
        strict.import_definition(metric, "strict-shrink.yaml")
    assert excinfo.value.code == "impact_blocked"
    assert RISK_IMPL_SHRINK in excinfo.value.details["risk_reasons"]

    after = _registry_counts(registry_conn)
    assert before == after  # no new metric_versions / metrics rows
    assert get_latest(registry_conn, "active_users")["version"] == 1
    audit = registry_conn.execute(
        "SELECT action, status FROM audit_log WHERE action = 'metric.import.blocked'"
    ).fetchall()
    assert len(audit) == 1 and audit[0]["status"] == "blocked"


def test_strict_mode_blocks_sql_change_in_directory_import(
    registry_conn, fixture_tree, sql_roots
):
    sql_path = fixture_tree / "impls" / "active_users_impl_a.sql"
    original = sql_path.read_text(encoding="utf-8")
    candidate_dir = fixture_tree / "candidate"
    try:
        sql_path.write_text(original + "\n-- strict sql change\n", encoding="utf-8")
        _write_metric(candidate_dir, _active_users(fixture_tree))
        before = _registry_counts(registry_conn)
        summary = MetricRegistry(registry_conn, sql_roots, strict=True).import_directory(
            candidate_dir
        )
    finally:
        sql_path.write_text(original, encoding="utf-8")

    assert summary.imported == []
    assert len(summary.errors) == 1
    assert summary.errors[0]["error"] == "impact_blocked"
    assert summary.high_risk_count == 1
    assert _registry_counts(registry_conn) == before
    assert get_latest(registry_conn, "active_users")["version"] == 1
    blocked = registry_conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE action = 'metric.import.blocked'"
    ).fetchone()[0]
    assert blocked == 1


def test_strict_mode_allows_low_risk_change(
    registry_conn, fixture_tree, sql_roots
):
    metric = _active_users(fixture_tree)
    metric.impls.append(
        ImplementDef("impl_c", "users_a", "active_users_impl_a.sql")
    )
    strict = MetricRegistry(registry_conn, sql_roots, strict=True)
    version = strict.import_definition(metric, "append.yaml")
    assert version == 2
    assert get_latest(registry_conn, "active_users")["version"] == 2


# ---------------------------------------------------------------------------
# Lenient mode: imports normally and carries the preview hint
# ---------------------------------------------------------------------------
def test_lenient_mode_imports_high_risk_with_preview_hint(
    registry_conn, fixture_tree, sql_roots
):
    sql_path = fixture_tree / "impls" / "active_users_impl_a.sql"
    original = sql_path.read_text(encoding="utf-8")
    candidate_dir = fixture_tree / "candidate"
    try:
        sql_path.write_text(original + "\n-- lenient sql change\n", encoding="utf-8")
        _write_metric(candidate_dir, _active_users(fixture_tree))
        summary = MetricRegistry(registry_conn, sql_roots).import_directory(candidate_dir)
    finally:
        sql_path.write_text(original, encoding="utf-8")

    assert summary.ok
    assert len(summary.imported) == 1
    result = summary.imported[0]
    assert result.version == 2
    assert result.preview["high_risk"] is True
    assert RISK_SQL_FINGERPRINT in result.preview["risk_reasons"]
    assert summary.previews[0]["high_risk"] is True
    assert get_latest(registry_conn, "active_users")["version"] == 2


# ---------------------------------------------------------------------------
# Both modes compute the same verdict
# ---------------------------------------------------------------------------
def test_strict_and_lenient_produce_identical_verdict(
    registry_conn, fixture_tree, sql_roots
):
    sql_path = fixture_tree / "impls" / "active_users_impl_a.sql"
    original = sql_path.read_text(encoding="utf-8")
    try:
        sql_path.write_text(original + "\n-- verdict parity\n", encoding="utf-8")
        metric = _active_users(fixture_tree)
        lenient = MetricRegistry(registry_conn, sql_roots, strict=False)
        strict = MetricRegistry(registry_conn, sql_roots, strict=True)
        a = lenient.preview_definition(metric).to_dict()
        b = strict.preview_definition(metric).to_dict()
    finally:
        sql_path.write_text(original, encoding="utf-8")
    assert a == b
    assert a["high_risk"] is True


# ---------------------------------------------------------------------------
# Read-only guarantee and repeatability
# ---------------------------------------------------------------------------
def test_preview_never_writes(registry_conn, fixture_tree, sql_roots):
    before = _registry_counts(registry_conn)
    before_audit = table_counts(registry_conn)["audit_log"]
    registry = MetricRegistry(registry_conn, sql_roots)
    metric = _active_users(fixture_tree)
    registry.preview_definition(metric)
    candidate = fixture_tree / "preview-only"
    _write_metric(candidate, metric)
    registry.preview_directory(candidate)
    assert _registry_counts(registry_conn) == before
    # Read-only previews must not even touch the audit trail.
    assert table_counts(registry_conn)["audit_log"] == before_audit


def test_repeated_preview_is_stable(registry_conn, fixture_tree, sql_roots):
    metric = _active_users(fixture_tree)
    registry = MetricRegistry(registry_conn, sql_roots)
    candidate = fixture_tree / "preview-only"
    path = _write_metric(candidate, metric)
    first = registry.preview_file(path)
    second = registry.preview_file(path)
    assert first == second


# ---------------------------------------------------------------------------
# Legacy / degraded rows: missing source_file or unreadable historical SQL
# ---------------------------------------------------------------------------
def test_legacy_row_without_source_file_degrades_by_key_and_impl_set(
    registry_conn, fixture_tree, sql_roots
):
    # Simulate an old metric_versions row that predates source_file metadata.
    registry_conn.execute(
        "UPDATE metric_versions SET source_file = '' WHERE metric_key = 'active_users'"
    )
    registry_conn.commit()

    metric = _active_users(fixture_tree)
    preview = build_impact_preview(
        registry_conn, metric, sql_roots
    ).to_dict()
    assert preview["degraded"] is True
    assert any("source_file" in w for w in preview["warnings"])
    # Still able to compare impl id sets: no change -> not high risk.
    assert preview["changed"] is False
    assert preview["high_risk"] is False


def test_legacy_row_whose_sql_disappears_degrades_without_crash(
    registry_conn, fixture_tree, sql_roots
):
    sql_path = fixture_tree / "impls" / "active_users_impl_a.sql"
    original = sql_path.read_text(encoding="utf-8")
    try:
        # Remove a historical SQL file: fingerprint for the "before" side is
        # unavailable, so the engine must degrade rather than raise.
        sql_path.unlink()
        metric = _active_users(fixture_tree)
        preview = build_impact_preview(
            registry_conn, metric, sql_roots
        ).to_dict()
        assert preview["degraded"] is True
        assert preview["warnings"]
        assert preview["high_risk"] is False
    finally:
        sql_path.write_text(original, encoding="utf-8")


def test_legacy_disk_fallback_detects_field_change(
    registry_conn, fixture_tree, sql_roots
):
    import json

    # Strip embedded fingerprints so the before-side falls back to disk reads.
    for table, key_col in (
        ("metric_versions", "metric_key"),
        ("metrics", "key"),
    ):
        row = registry_conn.execute(
            f"SELECT definition_json FROM {table} WHERE {key_col} = 'active_users'"
        ).fetchone()
        if row is None:
            continue
        definition = json.loads(row["definition_json"])
        for impl in definition["impls"]:
            impl.pop("sql_fingerprint", None)
        registry_conn.execute(
            f"UPDATE {table} SET definition_json = ? WHERE {key_col} = 'active_users'",
            (json.dumps(definition, ensure_ascii=False, sort_keys=True),),
        )
    registry_conn.commit()

    # Point impl_a at a different existing SQL file -> field change must still
    # be surfaced even though embedded fingerprints are absent.
    metric = _active_users(fixture_tree)
    metric.impls[0].sql_file = "active_users_impl_b.sql"
    preview = build_impact_preview(
        registry_conn, metric, sql_roots
    ).to_dict()
    assert preview["degraded"] is True
    modified = {item["impl_id"]: item for item in preview["modified_impls"]}
    assert "sql_file" in modified["impl_a"]["field_changes"]


def test_field_level_source_and_sql_file_changes_are_reported(
    registry_conn, fixture_tree, sql_roots
):
    metric = _active_users(fixture_tree)
    metric.impls[0].source = "users_b"
    preview = build_impact_preview(
        registry_conn, metric, sql_roots
    ).to_dict()
    modified = {item["impl_id"]: item for item in preview["modified_impls"]}
    assert "source" in modified["impl_a"]["field_changes"]
    assert modified["impl_a"]["field_changes"]["source"]["after"] == "users_b"
    # A source-only edit is not part of the high-risk rule set.
    assert preview["high_risk"] is False


def test_preview_directory_reports_parse_error_per_file(
    registry_conn, fixture_tree, sql_roots
):
    bad_dir = fixture_tree / "bad-metrics"
    bad_dir.mkdir(parents=True, exist_ok=True)
    (bad_dir / "broken.yaml").write_text("key: [unclosed\n", encoding="utf-8")
    previews = MetricRegistry(registry_conn, sql_roots).preview_directory(bad_dir)
    assert len(previews) == 1
    assert previews[0]["key"] is None
    assert previews[0]["error"]["code"] == "yaml_syntax_error"


def test_preview_directory_requires_existing_yaml_dir(
    registry_conn, fixture_tree, sql_roots
):
    from app.errors import CaliberError

    with pytest.raises(CaliberError):
        MetricRegistry(registry_conn, sql_roots).preview_directory(
            fixture_tree / "no-such-dir"
        )
    empty = fixture_tree / "empty-metrics"
    empty.mkdir()
    with pytest.raises(CaliberError):
        MetricRegistry(registry_conn, sql_roots).preview_directory(empty)


def test_sql_fingerprint_empty_path_returns_warning(sql_roots):
    fp, warn = sql_fingerprint("", sql_roots)
    assert fp is None and warn


def test_directory_import_validation_error_does_not_write_version(
    registry_conn, fixture_tree, sql_roots
):
    # An impl whose SQL file does not exist fails validation in lenient mode;
    # the error is collected and no version is appended.
    candidate = fixture_tree / "invalid-candidate"
    metric = _active_users(fixture_tree)
    metric.impls[0].sql_file = "missing_file.sql"
    _write_metric(candidate, metric)
    before = _registry_counts(registry_conn)
    summary = MetricRegistry(registry_conn, sql_roots).import_directory(candidate)
    assert summary.imported == []
    assert summary.errors[0]["error"] == "validation_error"
    assert _registry_counts(registry_conn) == before


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------
def test_cli_preview_metrics_is_read_only(
    tmp_path: Path, fixture_tree: Path, db_path: Path
):
    from cli.main import main

    prefix = ["--db", str(db_path), "--fixtures-dir", str(fixture_tree)]
    # Import once, then run two comparisons to build history.
    assert main(prefix + ["import-metrics", "--dir", str(fixture_tree / "metrics")]) == 0
    assert main(
        prefix + ["run", "--metrics", "active_users",
                  "--datasets", str(fixture_tree / "datasets")]
    ) == 0

    from app.db import connect
    conn = connect(db_path)
    try:
        before = table_counts(conn)
    finally:
        conn.close()

    code = main(prefix + ["preview-metrics", "--dir", str(fixture_tree / "metrics")])
    assert code == 0

    conn = connect(db_path)
    try:
        assert table_counts(conn) == before
        assert len(list_versions(conn, "active_users")) == 1
    finally:
        conn.close()


def test_cli_preview_json_output_and_strict_block(
    tmp_path: Path, fixture_tree: Path, db_path: Path, capsys
):
    from cli.main import main

    prefix = ["--db", str(db_path), "--fixtures-dir", str(fixture_tree)]
    assert main(prefix + ["import-metrics", "--dir", str(fixture_tree / "metrics")]) == 0
    capsys.readouterr()

    # Build a 3-impl baseline so that shrinking it to 2 impls still satisfies
    # the independent min-items rule; the high-risk verdict is then purely
    # driven by the impl-set contraction, not by validation failure.
    baseline_dir = tmp_path / "baseline"
    base_metric = _active_users(fixture_tree)
    base_metric.impls.append(
        ImplementDef("impl_c", "users_a", "active_users_impl_a.sql")
    )
    _write_metric(baseline_dir, base_metric)
    assert main(prefix + ["import-metrics", "--dir", str(baseline_dir)]) == 0
    capsys.readouterr()

    candidate = tmp_path / "candidate"
    shrunk = _active_users(fixture_tree)  # back to 2 impls: impl_a + impl_b
    _write_metric(candidate, shrunk)

    code = main(prefix + ["preview-metrics", "--dir", str(candidate), "--json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["high_risk"] is True
    assert payload[0]["removed_impls"] == ["impl_c"]

    # Strict import must fail with a non-zero exit and leave the library intact.
    exit_code = main(
        prefix + ["import-metrics", "--dir", str(candidate), "--strict"]
    )
    assert exit_code == 1
    captured = capsys.readouterr()
    assert "impact_blocked" in captured.err

    from app.db import connect
    conn = connect(db_path)
    try:
        assert get_latest(conn, "active_users")["version"] == 2
    finally:
        conn.close()

    # Lenient import of the same high-risk file still succeeds (still 2 impls).
    exit_code = main(prefix + ["import-metrics", "--dir", str(candidate)])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "预览提示:高风险" in out
    conn = connect(db_path)
    try:
        assert get_latest(conn, "active_users")["version"] == 3
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# HTTP preview endpoint
# ---------------------------------------------------------------------------
def test_preview_api_is_read_only(imported_client, app_config, tmp_path: Path):
    candidate = tmp_path / "candidate"
    metric = parse_metric_file(app_config.metrics_dir / "active_users.yaml")
    metric.impls.append(
        ImplementDef("impl_c", "users_a", "active_users_impl_a.sql")
    )
    _write_metric(candidate, metric)

    response = imported_client.post(
        "/api/metrics/preview",
        json={"metrics_dir": str(candidate)},
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["read_only"] is True
    preview = payload["previews"][0]
    assert preview["added_impls"] == ["impl_c"]
    assert payload["high_risk_count"] == 0

    # The endpoint must not have created a new version.
    detail = imported_client.get("/api/metrics/active_users").get_json()
    assert detail["metric"]["version"] == 1


def test_preview_api_flags_high_risk(imported_client, app_config, tmp_path: Path):
    # The HTTP app reads SQL from app_config.fixtures_dir (the conftest copy),
    # so the content edit must target that copy, not the project fixtures.
    sql_path = app_config.fixtures_dir / "impls" / "active_users_impl_a.sql"
    original = sql_path.read_text(encoding="utf-8")
    candidate = tmp_path / "candidate"
    try:
        sql_path.write_text(original + "\n-- api high risk\n", encoding="utf-8")
        _write_metric(
            candidate,
            parse_metric_file(app_config.metrics_dir / "active_users.yaml"),
        )
        response = imported_client.post(
            "/api/metrics/preview",
            json={"metrics_dir": str(candidate)},
        )
    finally:
        sql_path.write_text(original, encoding="utf-8")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["high_risk_count"] == 1
    assert payload["high_risk_keys"] == ["active_users"]


def test_preview_api_validates_payload(imported_client):
    response = imported_client.post("/api/metrics/preview", json={})
    assert response.status_code == 400
    assert response.get_json()["error"] == "caliber_error"
