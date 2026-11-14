from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from app.audit import log_audit
from app.core.dataset_loader import DatasetCatalog
from app.core.dsl_parser import parse_metric_file
from app.core.reporter import build_report, export_report
from app.core.snapshot import (
    create_run,
    detect_drift,
    get_snapshot,
    list_snapshots,
    perform_run,
    save_snapshot,
    snapshots_for_run,
)
from app.errors import NotFoundError
from app.models import (
    ComparisonResult,
    DimensionDiff,
    ImplEvaluation,
    MetricDefinition,
)


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



def _metric_with_non_finite_values(registry_conn) -> int:
    metric = MetricDefinition(
        key="edge_values",
        name="极值指标",
        owner="qa",
        description="含非有限值",
        unit="x",
        grain="day",
        impls=[],
    )
    run_id = create_run(registry_conn, ["edge_values"], "/tmp/datasets", {"abs": 0.0, "rel": 0.0})
    diff = DimensionDiff(
        dimension="d1",
        kind="value_mismatch",
        values={"impl_a": math.inf, "impl_b": -math.inf, "impl_c": math.nan},
        max_delta=math.inf,
        detail="数值超出容差",
    )
    comparison = ComparisonResult(
        metric_key="edge_values",
        metric_version=1,
        impl_ids=["impl_a", "impl_b", "impl_c"],
        verdict="inconsistent",
        dimensions=["d1"],
        diffs=[diff],
        summary={
            "impl_count": 3,
            "dimension_count": 1,
            "consistent_dimensions": 0,
            "value_mismatches": 1,
            "missing_dimensions": 0,
            "diff_count": 1,
        },
        abs_tolerance=0.0,
        rel_tolerance=0.0,
    )
    evaluations = {
        "impl_a": ImplEvaluation("impl_a", "ds_a", "a.sql", {"d1": math.inf}, 1, 1.0),
        "impl_b": ImplEvaluation("impl_b", "ds_b", "b.sql", {"d1": -math.inf}, 1, 1.0),
        "impl_c": ImplEvaluation("impl_c", "ds_c", "c.sql", {"d1": math.nan}, 1, 1.0),
    }
    save_snapshot(registry_conn, run_id, metric, comparison, evaluations)
    return run_id


def test_json_export_serializes_non_finite_values_as_strict_json(registry_conn):
    run_id = _metric_with_non_finite_values(registry_conn)
    report = build_report(registry_conn, run_id)
    fmt, raw = export_report(report, "json")
    assert fmt == "json"
    # 严格解析：不允许 NaN / Infinity / -Infinity token
    decoded = json.loads(
        raw,
        parse_constant=lambda token: pytest.fail(f"出现非法 JSON 常量: {token}"),
    )
    metric = next(item for item in decoded["metrics"] if item["key"] == "edge_values")
    diff = metric["diffs"][0]
    assert diff["values"] == {
        "impl_a": "Infinity",
        "impl_b": "-Infinity",
        "impl_c": "NaN",
    }
    assert diff["max_delta"] == "Infinity"
    # 原始文本里也不得出现裸 token
    assert " Infinity" not in raw.replace('"Infinity"', "")
    assert " NaN" not in raw.replace('"NaN"', "")


def test_non_finite_values_render_consistently_in_md_and_html(registry_conn):
    run_id = _metric_with_non_finite_values(registry_conn)
    report = build_report(registry_conn, run_id)
    _, markdown = export_report(report, "md")
    _, page = export_report(report, "html")
    for token in ("Infinity", "-Infinity", "NaN"):
        assert token in markdown
        assert token in page


def test_same_run_exports_identically_across_formats_and_selectors(
    registry_conn, execute_fixture_run
):
    run_id = execute_fixture_run()
    # 连续导出多次并混入审计写入，报告实质内容必须稳定
    log_audit(registry_conn, "report.export", f"run:{run_id}", "ok", {"format": "json"})
    first = build_report(registry_conn, run_id)
    log_audit(registry_conn, "report.export", f"run:{run_id}", "ok", {"format": "md"})
    second = build_report(registry_conn, run_id)
    assert first["generated_at"] == second["generated_at"]
    assert first["audit"] == second["audit"]
    assert first["summary"] == second["summary"]
    assert first["metrics"] == second["metrics"]
    assert first["run"] == second["run"]

    json_a = export_report(first, "json")[1]
    json_b = export_report(second, "json")[1]
    assert json_a == json_b

    def fingerprint(report: dict) -> tuple:
        return (
            report["summary"],
            [
                (
                    item["key"],
                    item["verdict"],
                    item["summary"],
                    [(d["dimension"], d["kind"], d["values"], d["max_delta"])
                     for d in item["diffs"]],
                )
                for item in report["metrics"]
            ],
        )

    md_report = build_report(registry_conn, run_id)
    html_report = build_report(registry_conn, run_id)
    assert fingerprint(md_report) == fingerprint(html_report) == fingerprint(first)


def test_json_safe_handles_nested_and_non_finite_values():
    from app.core.reporter import json_safe, _cell

    payload = {
        "finite": 1.25,
        "inf": math.inf,
        "neg_inf": -math.inf,
        "nan": math.nan,
        "nested": [{"delta": math.inf}, 3, None],
        "text": "ok",
    }
    safe = json_safe(payload)
    assert safe["finite"] == 1.25
    assert safe["inf"] == "Infinity"
    assert safe["neg_inf"] == "-Infinity"
    assert safe["nan"] == "NaN"
    assert safe["nested"][0]["delta"] == "Infinity"
    assert safe["nested"][1:] == [3, None]
    assert safe["text"] == "ok"
    # 转换后必须能被严格 JSON 序列化
    json.dumps(safe, allow_nan=False)
    assert _cell(None) == "缺失"
    assert _cell(math.inf) == "Infinity"
    assert _cell(math.nan) == "NaN"
    assert _cell(1.0 / 3.0)  # 普通浮点仍按 6 位有效数字渲染


def test_drift_events_render_in_all_formats(
    registry_conn, fixture_tree, sql_roots, execute_fixture_run
):
    first_run = execute_fixture_run(["active_users"])
    first = snapshots_for_run(registry_conn, first_run)[0]
    assert first["verdict"] == "consistent"
    users_b = fixture_tree / "datasets" / "users_b.csv"
    users_b.write_text(
        users_b.read_text(encoding="utf-8") + "2008,2026-01-20,2026-02-01,active\n",
        encoding="utf-8",
    )
    catalog = DatasetCatalog(fixture_tree / "datasets")
    try:
        second_run = perform_run(
            registry_conn, ["active_users"], catalog, sql_roots,
            datasets_dir=fixture_tree / "datasets", abs_tolerance=1e-6, rel_tolerance=0,
        )
    finally:
        catalog.close()
    report = build_report(registry_conn, second_run)
    assert report["summary"]["drift_count"] == 1
    _, markdown = export_report(report, "md")
    _, page = export_report(report, "html")
    _, raw_json = export_report(report, "json")
    assert "active_users" in markdown and "漂移" in markdown
    assert "active_users" in page and "<li>" in page
    assert json.loads(raw_json)["drift_events"][0]["metric_key"] == "active_users"
