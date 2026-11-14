from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from app.core.dataset_loader import DatasetCatalog
from app.core.dsl_parser import parse_metric_file
from app.core.reporter import build_report, export_report
from app.core.snapshot import (
    detect_drift,
    get_run,
    get_snapshot,
    list_snapshots,
    normalize_dataset_refs,
    perform_run,
    snapshots_for_run,
)
from app.errors import NotFoundError


def test_all_fixture_metrics_produce_consistent_and_inconsistent_snapshots(
    execute_fixture_run, registry_conn
):
    run_id = execute_fixture_run()
    snapshots = snapshots_for_run(registry_conn, run_id)
    assert len(snapshots) == 3
    verdicts = {item["metric_key"]: item["verdict"] for item in snapshots}
    assert verdicts == {
        "active_users": "consistent",
        "avg_order_value": "inconsistent",
        "conversion_rate": "consistent",
    }
    assert set(list_snapshots(registry_conn, limit=10)[0]["impl_ids"]) == {"impl_a", "impl_b"}
    assert get_snapshot(registry_conn, snapshots[0]["id"])["result"]["metric"]["name"]


def test_snapshot_missing_raises(registry_conn):
    with pytest.raises(NotFoundError):
        get_snapshot(registry_conn, 999)


def test_two_runs_detect_consistent_to_inconsistent_drift(
    registry_conn,
    fixture_tree: Path,
    sql_roots: tuple[Path, ...],
    execute_fixture_run,
):
    first_run = execute_fixture_run(["active_users"])
    first = snapshots_for_run(registry_conn, first_run)[0]
    assert first["verdict"] == "consistent"
    users_b = fixture_tree / "datasets" / "users_b.csv"
    users_b.write_text(
        users_b.read_text(encoding="utf-8")
        + "2008,2026-01-20,2026-02-01,active\n",
        encoding="utf-8",
    )
    catalog = DatasetCatalog(fixture_tree / "datasets")
    try:
        second_run = perform_run(
            registry_conn,
            ["active_users"],
            catalog,
            sql_roots,
            datasets_dir=fixture_tree / "datasets",
            abs_tolerance=1e-6,
            rel_tolerance=0,
        )
    finally:
        catalog.close()
    second = snapshots_for_run(registry_conn, second_run)[0]
    assert second["verdict"] == "inconsistent"
    events = detect_drift(registry_conn, "active_users")
    assert len(events) == 1
    assert events[0]["from_snapshot_id"] == first["id"]
    assert events[0]["to_snapshot_id"] == second["id"]
    assert events[0]["from_verdict"] == "consistent"
    assert events[0]["to_verdict"] == "inconsistent"


def test_manual_snapshot_and_report_formats(registry_conn, execute_fixture_run, fixture_tree):
    run_id = execute_fixture_run()
    report = build_report(registry_conn, run_id)
    assert report["run"]["id"] == run_id
    assert report["summary"]["metric_count"] == 3
    assert report["summary"]["inconsistent"] == 1
    assert report["summary"]["drift_count"] == 0
    assert len(report["metrics"]) == 3
    avg = next(item for item in report["metrics"] if item["key"] == "avg_order_value")
    assert avg["verdict"] == "inconsistent"
    assert avg["diffs"]
    assert {"impl_a", "impl_b"} == {item["impl_id"] for item in avg["implementations"]}
    assert report["audit"]

    fmt, raw_json = export_report(report, "json")
    decoded = json.loads(raw_json)
    assert fmt == "json"
    assert decoded["run"]["id"] == run_id
    assert "metrics" in decoded and "drift_events" in decoded

    fmt, markdown = export_report(report, "md")
    assert fmt == "md"
    assert markdown.startswith("# 指标准星一致性报告")
    assert "avg_order_value" in markdown
    assert "inconsistent" in markdown
    assert "## 审计信息" in markdown
    assert "不一致维度" in markdown

    fmt, rendered_html = export_report(report, "html")
    assert fmt == "html"
    assert rendered_html.startswith("<!doctype html>")
    assert "指标准星一致性报告" in rendered_html
    assert "http://" not in rendered_html
    assert "https://" not in rendered_html
    assert "<script" not in rendered_html



def _run_with_fresh_catalog(registry_conn, fixture_tree, sql_roots, keys=None):
    catalog = DatasetCatalog(fixture_tree / "datasets")
    try:
        return perform_run(
            registry_conn,
            keys,
            catalog,
            sql_roots,
            datasets_dir=fixture_tree / "datasets",
            timeout_seconds=2.0,
            max_rows=100,
        )
    finally:
        catalog.close()


def test_missing_dataset_degrades_only_affected_metric(
    registry_conn, fixture_tree: Path, sql_roots
):
    missing = fixture_tree / "datasets" / "users_b.csv"
    hidden = fixture_tree / "users_b.csv.hidden"
    shutil.move(missing, hidden)
    try:
        run_id = _run_with_fresh_catalog(registry_conn, fixture_tree, sql_roots)
        record = get_run(registry_conn, run_id)
        assert record["status"] == "completed"
        snaps = {item["metric_key"]: item for item in snapshots_for_run(registry_conn, run_id)}
        assert snaps["active_users"]["verdict"] == "unevaluable"
        assert snaps["avg_order_value"]["verdict"] == "inconsistent"
        assert snaps["conversion_rate"]["verdict"] == "consistent"
        blocked = snaps["active_users"]["result"]["unevaluable_impls"]
        assert blocked and blocked[0]["impl_id"] == "impl_b"
        assert "users_b.csv" in blocked[0]["reason"]
        # Snapshot references only datasets that were actually read.
        manifest_paths = {item["rel_path"] for item in record["datasets_manifest"]}
        assert "users_b.csv" not in manifest_paths
        for snap in snaps.values():
            assert {item["rel_path"] for item in snap["datasets"]} <= manifest_paths
        assert {item["key"] for item in snaps["active_users"]["datasets"]} == {
            "users_a"
        }
    finally:
        shutil.move(hidden, missing)


def test_all_export_formats_share_snapshot_dataset_sequence(
    registry_conn, fixture_tree: Path, sql_roots
):
    run_id = _run_with_fresh_catalog(registry_conn, fixture_tree, sql_roots)
    report = build_report(registry_conn, run_id)
    manifest_seq = [item["rel_path"] for item in report["datasets_manifest"]]
    assert manifest_seq  # non-empty and frozen at run time
    fmt_json = export_report(report, "json")[1]
    decoded = json.loads(fmt_json)
    assert [item["rel_path"] for item in decoded["datasets_manifest"]] == manifest_seq
    for metric in decoded["metrics"]:
        seq = [item["rel_path"] for item in metric["datasets"]]
        assert seq == sorted(seq)
    markdown = export_report(report, "md")[1]
    html_doc = export_report(report, "html")[1]
    for rel in manifest_seq:
        assert rel in markdown and rel in html_doc
    # Re-exporting is byte-identical apart from the generated timestamp.
    again = build_report(registry_conn, run_id)
    assert export_report(again, "md")[1] == markdown
    first = json.loads(
        export_report(report, "json")[1].replace(
            report["generated_at"], "TS"
        )
    )
    second = json.loads(
        export_report(again, "json")[1].replace(
            again["generated_at"], "TS"
        )
    )
    assert first == second


def test_legacy_string_dataset_refs_are_upgraded_with_note(registry_conn):
    refs = normalize_dataset_refs(["users_a", "orders_b"])
    assert [item["key"] for item in refs] == ["users_a", "orders_b"]
    assert refs[0]["rel_path"] == "users_a.csv"
    assert all(item.get("note") for item in refs)
    # New-style references pass through untouched and keep order.
    fresh = normalize_dataset_refs(
        [{"key": "sub__users_a", "name": "users_a", "rel_path": "sub/users_a.csv"}]
    )
    assert fresh[0]["key"] == "sub__users_a"
    assert "note" not in fresh[0]
    assert normalize_dataset_refs(None) == []


def test_unevaluable_snapshot_renders_in_every_format(
    registry_conn, fixture_tree: Path, sql_roots
):
    hidden = fixture_tree / "users_b.csv.hidden"
    shutil.move(fixture_tree / "datasets" / "users_b.csv", hidden)
    try:
        run_id = _run_with_fresh_catalog(
            registry_conn, fixture_tree, sql_roots, ["active_users"]
        )
        report = build_report(registry_conn, run_id)
        assert report["summary"]["unevaluable"] == 1
        for fmt in ("json", "md", "html"):
            content = export_report(report, fmt)[1]
            assert "unevaluable" in content
            assert "users_b.csv" in content
    finally:
        shutil.move(hidden, fixture_tree / "datasets" / "users_b.csv")
