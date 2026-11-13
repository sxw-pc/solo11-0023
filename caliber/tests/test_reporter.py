from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.dataset_loader import DatasetCatalog
from app.core.dsl_parser import parse_metric_file
from app.core.reporter import build_report, export_report
from app.core.snapshot import (
    detect_drift,
    get_snapshot,
    list_snapshots,
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

