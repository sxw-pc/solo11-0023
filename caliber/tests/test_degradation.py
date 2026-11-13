"""Batch degradation: timeout / row-cap isolation, counts, snapshots, reports."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.evaluator import (
    evaluate_sql,
    run_metric_isolated,
)
from app.core.reporter import build_report, export_report
from app.core.snapshot import (
    DEGRADATION_POLICY,
    detect_drift,
    get_run,
    is_degraded_snapshot,
    perform_run,
    snapshots_for_run,
)
from app.core.registry import MetricRegistry
from app.core.dsl_parser import parse_metric_file
from app.core.dataset_loader import DatasetCatalog
from app.db import init_db
from app.errors import EvaluationRowLimit, EvaluationTimeout
from cli.main import main as cli_main

SLOW_SQL = (
    "WITH RECURSIVE cnt(x) AS (VALUES(1) UNION ALL "
    "SELECT x + 1 FROM cnt WHERE x < 100000000) SELECT 'all', COUNT(*) FROM cnt"
)


@pytest.fixture
def degradation_tree(fixture_tree: Path) -> Path:
    """Add one timeout metric and one row-cap metric to the fixture tree."""
    impls = fixture_tree / "impls"
    metrics = fixture_tree / "metrics"
    (impls / "slow_impl_a.sql").write_text(SLOW_SQL, encoding="utf-8")
    (impls / "slow_impl_b.sql").write_text(
        "SELECT 'all', COUNT(*) FROM users_b", encoding="utf-8"
    )
    (metrics / "slow_metric.yaml").write_text(
        "key: slow_metric\n"
        "name: 超时指标\n"
        "owner: demo\n"
        "description: 故意运行很久的实现。\n"
        "unit: 人\n"
        "grain: day\n"
        "impls:\n"
        "  - id: impl_a\n"
        "    source: users_a\n"
        "    sql_file: slow_impl_a.sql\n"
        "  - id: impl_b\n"
        "    source: users_b\n"
        "    sql_file: slow_impl_b.sql\n",
        encoding="utf-8",
    )
    (impls / "rows_impl_a.sql").write_text(
        "SELECT order_id, SUM(amount) FROM orders_a GROUP BY order_id",
        encoding="utf-8",
    )
    (impls / "rows_impl_b.sql").write_text(
        "SELECT order_id, SUM(amount) FROM orders_b GROUP BY order_id",
        encoding="utf-8",
    )
    (metrics / "rows_metric.yaml").write_text(
        "key: rows_metric\n"
        "name: 行数指标\n"
        "owner: demo\n"
        "description: 超过结果行数上限。\n"
        "unit: 元\n"
        "grain: day\n"
        "impls:\n"
        "  - id: impl_a\n"
        "    source: orders_a\n"
        "    sql_file: rows_impl_a.sql\n"
        "  - id: impl_b\n"
        "    source: orders_b\n"
        "    sql_file: rows_impl_b.sql\n",
        encoding="utf-8",
    )
    return fixture_tree


@pytest.fixture
def degradation_conn(conn, degradation_tree: Path):
    MetricRegistry(conn, (degradation_tree / "impls",)).import_directory(
        degradation_tree / "metrics"
    )
    return conn


@pytest.fixture
def mixed_run(degradation_conn, degradation_tree: Path):
    catalog = DatasetCatalog(degradation_tree / "datasets")
    try:
        run_id = perform_run(
            degradation_conn,
            ["active_users", "slow_metric", "rows_metric", "avg_order_value"],
            catalog,
            (degradation_tree / "impls",),
            datasets_dir=degradation_tree / "datasets",
            timeout_seconds=0.05,
            max_rows=5,
        )
    finally:
        catalog.close()
    return run_id


def test_mixed_batch_does_not_abort_and_reports_three_counts(mixed_run, degradation_conn):
    run = get_run(degradation_conn, mixed_run)
    assert run["status"] == "completed"
    summary = run["summary"]
    assert summary["metrics"] == 4
    assert summary["completed"] == 2
    assert summary["degraded"] == 2
    assert summary["degraded_timeout"] == 1
    assert summary["degraded_row_limit"] == 1
    assert summary["failed"] == 0
    assert summary["consistent"] == 1
    assert summary["inconsistent"] == 1
    # All four metrics were attempted: a degraded metric cannot hide the ones
    # that follow it in the batch.
    snapshots = snapshots_for_run(degradation_conn, mixed_run)
    assert [item["metric_key"] for item in snapshots] == [
        "active_users",
        "slow_metric",
        "rows_metric",
        "avg_order_value",
    ]


def test_degraded_snapshots_are_marked_and_distinguishable(mixed_run, degradation_conn):
    snapshots = {
        item["metric_key"]: item
        for item in snapshots_for_run(degradation_conn, mixed_run)
    }
    timed_out = snapshots["slow_metric"]
    assert timed_out["verdict"] == "degraded"
    assert timed_out["summary"]["degraded"] is True
    assert timed_out["summary"]["degraded_reason"] == "timeout"
    assert timed_out["result"]["degraded"]["reason"] == "timeout"
    assert timed_out["result"]["degraded"]["failed_impl_id"] == "impl_a"
    assert timed_out["result"]["degraded"]["timeout_seconds"] == pytest.approx(0.05)

    truncated = snapshots["rows_metric"]
    assert truncated["verdict"] == "degraded"
    assert truncated["summary"]["degraded_reason"] == "row_limit"
    degradation = truncated["result"]["degraded"]
    assert degradation["max_rows"] == 5
    assert degradation["observed_rows"] == 10
    assert degradation["row_count_exact"] is True
    assert degradation["failed_impl_id"] == "impl_a"

    # Normal snapshots keep the historical shape and do not look degraded.
    normal = snapshots["active_users"]
    assert normal["verdict"] == "consistent"
    assert "degraded" not in normal["summary"] or normal["summary"]["degraded"] is False


def test_execution_metadata_records_policy_and_detection_path(mixed_run, degradation_conn):
    run = get_run(degradation_conn, mixed_run)
    execution = run["summary"]["execution"]
    assert execution["limits"] == {
        "timeout_seconds": pytest.approx(0.05),
        "max_rows": 5,
        "scope": "per_impl",
    }
    policy = execution["degradation"]
    assert policy["policy"] == DEGRADATION_POLICY["policy"]
    assert policy["snapshot_persisted"] is True
    assert policy["reason"]
    assert policy["downstream_detection"]["snapshot_verdict"] == "degraded"
    assert policy["downstream_detection"]["summary_flag"] == "summary.degraded == true"


def test_report_exports_degraded_metrics_and_markers(mixed_run, degradation_conn):
    report = build_report(degradation_conn, mixed_run)
    assert report["summary"]["degraded_timeout"] == 1
    assert report["summary"]["degraded_row_limit"] == 1
    assert report["summary"]["execution"]["limits"]["scope"] == "per_impl"
    degraded = [item for item in report["metrics"] if item["degraded"]]
    assert {item["degraded_reason"] for item in degraded} == {"timeout", "row_limit"}

    fmt, raw = export_report(report, "json")
    decoded = json.loads(raw)
    assert fmt == "json"
    assert decoded["summary"]["degraded_timeout"] == 1
    assert decoded["summary"]["degraded_row_limit"] == 1

    fmt, markdown = export_report(report, "md")
    assert "超时降级" in markdown and "截断降级" in markdown
    assert "slow_metric" in markdown and "degraded" in markdown
    assert "截断前实际读取 10 行" in markdown

    fmt, rendered = export_report(report, "html")
    assert "degraded" in rendered
    assert "截断前实际读取 10 行" in rendered


def test_two_identical_batches_are_stable_and_leak_free(degradation_conn, degradation_tree):
    keys = ["active_users", "slow_metric", "rows_metric", "avg_order_value"]

    def one_run() -> tuple[int, dict, list[dict]]:
        catalog = DatasetCatalog(degradation_tree / "datasets")
        try:
            run_id = perform_run(
                degradation_conn,
                keys,
                catalog,
                (degradation_tree / "impls",),
                datasets_dir=degradation_tree / "datasets",
                timeout_seconds=0.05,
                max_rows=5,
            )
        finally:
            catalog.close()
        run = get_run(degradation_conn, run_id)
        return run_id, run["summary"], snapshots_for_run(degradation_conn, run_id)

    first_id, first_summary, first_snapshots = one_run()
    second_id, second_summary, second_snapshots = one_run()
    assert first_id != second_id
    for field in (
        "consistent",
        "inconsistent",
        "partial",
        "completed",
        "degraded",
        "degraded_timeout",
        "degraded_row_limit",
        "failed",
    ):
        assert first_summary[field] == second_summary[field]
    assert [(item["metric_key"], item["verdict"]) for item in first_snapshots] == [
        (item["metric_key"], item["verdict"]) for item in second_snapshots
    ]
    per_run = [
        (item["metric_key"], item["summary"].get("degraded_reason"))
        for item in (*first_snapshots, *second_snapshots)
    ]
    assert per_run == [
        ("active_users", None),
        ("slow_metric", "timeout"),
        ("rows_metric", "row_limit"),
        ("avg_order_value", None),
        ("active_users", None),
        ("slow_metric", "timeout"),
        ("rows_metric", "row_limit"),
        ("avg_order_value", None),
    ]


def test_shared_connection_is_reusable_after_degradation(degradation_tree):
    catalog = DatasetCatalog(degradation_tree / "datasets")
    try:
        conn = catalog.readonly_connection()
        with pytest.raises(EvaluationTimeout):
            evaluate_sql(conn, SLOW_SQL, timeout_seconds=0.02, impl_id="a")
        assert conn.in_transaction is False
        # The same connection must still serve a fast query afterwards.
        values, rows, _ = evaluate_sql(conn, "SELECT COUNT(*) FROM users_a")
        assert rows == 1 and "__all__" in values
    finally:
        catalog.close()


def test_row_limit_counts_rows_beyond_cap_and_cleans_up(degradation_tree):
    catalog = DatasetCatalog(degradation_tree / "datasets")
    try:
        conn = catalog.readonly_connection()
        with pytest.raises(EvaluationRowLimit) as caught:
            evaluate_sql(
                conn,
                "SELECT order_id, SUM(amount) FROM orders_a GROUP BY order_id",
                max_rows=2,
            )
        assert caught.value.max_rows == 2
        assert caught.value.observed_rows == 10
        assert caught.value.row_count_exact is True
        assert conn.in_transaction is False
        assert conn.execute("SELECT COUNT(*) FROM orders_a").fetchone()[0] == 10
    finally:
        catalog.close()


def test_non_degradation_errors_still_abort_the_run(degradation_tree):
    """Only timeout/row-cap degrade; a genuinely broken definition fails."""
    from dataclasses import replace

    from app.errors import NotFoundError

    metric = parse_metric_file(degradation_tree / "metrics" / "active_users.yaml")
    # Point one implementation at a dataset that does not exist.  This is a
    # hard NotFoundError, not a degradation.
    broken = replace(
        metric,
        impls=[replace(metric.impls[0], source="missing_dataset"), metric.impls[1]],
    )
    catalog = DatasetCatalog(degradation_tree / "datasets")
    try:
        with pytest.raises(NotFoundError):
            run_metric_isolated(
                broken,
                1,
                catalog,
                (degradation_tree / "impls",),
                timeout_seconds=1.0,
                max_rows=100,
            )
    finally:
        catalog.close()


def test_degraded_snapshots_do_not_create_or_break_drift(mixed_run, degradation_conn):
    # active_users goes consistent, slow_metric / rows_metric are degraded and
    # must not appear as drift endpoints.
    events = detect_drift(degradation_conn, "slow_metric")
    assert events == []
    events = detect_drift(degradation_conn, "rows_metric")
    assert events == []
    # A real inconsistent snapshot in the same run still shows up nowhere as
    # drift (single run, no previous consistent snapshot).
    assert detect_drift(degradation_conn, "avg_order_value") == []


def test_is_degraded_snapshot_helper(mixed_run, degradation_conn):
    snapshots = {
        item["metric_key"]: item
        for item in snapshots_for_run(degradation_conn, mixed_run)
    }
    assert is_degraded_snapshot(snapshots["slow_metric"]) is True
    assert is_degraded_snapshot(snapshots["rows_metric"]) is True
    assert is_degraded_snapshot(snapshots["active_users"]) is False


def test_cli_mixed_batch_exits_zero_and_prints_degradation(
    tmp_path: Path, degradation_tree: Path, capsys
):
    db_path = tmp_path / "cli-degraded.db"
    prefix = ["--db", str(db_path), "--fixtures-dir", str(degradation_tree)]
    assert cli_main(prefix + ["import-metrics", "--dir", str(degradation_tree / "metrics")]) == 0
    capsys.readouterr()
    code = cli_main(
        prefix
        + [
            "run",
            "--metrics",
            "active_users,slow_metric,rows_metric",
            "--datasets",
            str(degradation_tree / "datasets"),
            "--timeout",
            "0.05",
            "--max-rows",
            "5",
        ]
    )
    assert code == 0
    output = capsys.readouterr().out
    assert "超时降级 1" in output
    assert "截断降级 1" in output
    assert "slow_metric" in output and "降级/超时" in output
    assert "rows_metric" in output and "降级/行数截断" in output
    assert "实际行数=10" in output and "上限=5" in output


def test_report_lists_all_metrics_even_when_degraded(mixed_run, degradation_conn):
    report = build_report(degradation_conn, mixed_run)
    assert report["summary"]["metric_count"] == 4
    keys = [item["key"] for item in report["metrics"]]
    assert keys == ["active_users", "slow_metric", "rows_metric", "avg_order_value"]
    for item in report["metrics"]:
        if item["degraded"]:
            assert item["status"] == "degraded"
            assert item["degradation"]["reason"] == item["degraded_reason"]
            assert item["implementations"]
        else:
            assert item["status"] == "completed"
            assert item["degraded_reason"] is None


def test_api_run_returns_degraded_snapshots(
    tmp_path: Path, degradation_tree: Path, degradation_conn
):
    from app import create_app
    from app.config import Config

    data_dir = tmp_path / "api-data"
    config = Config(
        root_dir=degradation_tree,
        data_dir=data_dir,
        db_path=data_dir / "caliber.sqlite3",
        fixtures_dir=degradation_tree,
        datasets_dir=degradation_tree / "datasets",
        metrics_dir=degradation_tree / "metrics",
        sql_roots=(degradation_tree / "impls",),
        eval_timeout_seconds=0.05,
        max_result_rows=5,
        testing=True,
    )
    connection = init_db(config.db_path)
    try:
        MetricRegistry(connection, config.sql_roots).import_directory(config.metrics_dir)
    finally:
        connection.close()
    client = create_app(config).test_client()
    response = client.post(
        "/api/runs",
        json={
            "metrics": ["active_users", "slow_metric", "rows_metric"],
            "datasets_dir": str(config.datasets_dir),
        },
    )
    assert response.status_code == 201
    payload = response.get_json()
    assert payload["run"]["status"] == "completed"
    assert payload["run"]["summary"]["degraded_timeout"] == 1
    assert payload["run"]["summary"]["degraded_row_limit"] == 1
    verdicts = {item["metric_key"]: item["verdict"] for item in payload["snapshots"]}
    assert verdicts == {
        "active_users": "consistent",
        "slow_metric": "degraded",
        "rows_metric": "degraded",
    }

    report = client.get(f"/api/reports/{payload['run']['id']}?format=json").get_json()
    assert report["summary"]["degraded_timeout"] == 1
    assert report["summary"]["degraded_row_limit"] == 1
