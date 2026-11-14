"""End-to-end attribution behavior through run/snapshot/report layers."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.dataset_loader import DatasetCatalog
from app.core.dsl_parser import parse_metric_text
from app.core.reporter import build_report, export_report
from app.core.snapshot import get_snapshot, perform_run, snapshots_for_run
from app.models import ImplementDef, MetricDefinition


def _open_catalog(fixture_tree: Path):
    catalog = DatasetCatalog(fixture_tree / "datasets")
    return catalog


def test_segment_revenue_run_produces_three_clusters(
    registry_conn, fixture_tree: Path, sql_roots
):
    catalog = _open_catalog(fixture_tree)
    try:
        run_id = perform_run(
            registry_conn,
            ["segment_revenue"],
            catalog,
            sql_roots,
            datasets_dir=fixture_tree / "datasets",
            timeout_seconds=2.0,
            max_rows=100,
            abs_tolerance=1e-6,
            rel_tolerance=0.0,
        )
    finally:
        catalog.close()

    snapshot = snapshots_for_run(registry_conn, run_id)[0]
    result = snapshot["result"]
    attribution = result["attribution"]
    assert attribution["status"] == "ok"
    assert attribution["natural_keys"] == ["region", "segment"]
    assert attribution["key_strategy"] == "dsl_declared"
    assert attribution["total_aligned_keys"] == 10
    assert attribution["affected_keys"] == 8
    clusters = attribution["clusters"]
    assert [c["diff_type"] for c in clusters] == [
        "only_one_side",
        "type_mismatch",
        "value_deviation",
    ]
    affected = {c["diff_type"]: c["affected_keys"] for c in clusters}
    assert affected == {"only_one_side": 1, "type_mismatch": 2, "value_deviation": 5}
    assert clusters[0]["representative_keys"] == [["east", "x1"]]
    # 代表键有界
    for cluster in clusters:
        assert len(cluster["representative_keys"]) <= 5
    # 明细暂存文件没有泄漏进快照
    assert all("detail_file" not in impl for impl in result["implementations"])

    # run 汇总里带归因状态计数
    from app.core.snapshot import get_run

    summary = get_run(registry_conn, run_id)["summary"]
    assert summary["attribution"]["ok"] == 1


def test_two_consecutive_runs_keep_stable_clusters(
    registry_conn, fixture_tree: Path, sql_roots
):
    def execute() -> dict:
        catalog = _open_catalog(fixture_tree)
        try:
            run_id = perform_run(
                registry_conn,
                ["segment_revenue"],
                catalog,
                sql_roots,
                datasets_dir=fixture_tree / "datasets",
                abs_tolerance=1e-6,
                rel_tolerance=0.0,
            )
        finally:
            catalog.close()
        return snapshots_for_run(registry_conn, run_id)[0]["result"]["attribution"]

    first = execute()
    second = execute()
    for key in ("clusters", "total_aligned_keys", "affected_keys", "natural_keys"):
        assert first[key] == second[key]


def test_missing_alignment_column_degrades_and_run_continues(
    registry_conn, fixture_tree: Path, sql_roots, tmp_path: Path
):
    # 声明了 natural_keys，但 SQL 结果缺列 -> 归因降级，维度结果仍落快照
    impls_dir = tmp_path / "impls"
    impls_dir.mkdir()
    (impls_dir / "bad_a.sql").write_text(
        "SELECT region, revenue FROM revenue_a ORDER BY region", encoding="utf-8"
    )
    (impls_dir / "bad_b.sql").write_text(
        "SELECT region, revenue FROM revenue_b ORDER BY region", encoding="utf-8"
    )
    metric = MetricDefinition(
        key="bad_alignment",
        name="缺列归因指标",
        owner="qa",
        description="声明复合键但结果缺 segment 列",
        unit="元",
        grain="day",
        natural_keys=["region", "segment"],
        impls=[
            ImplementDef("impl_a", "revenue_a", "bad_a.sql"),
            ImplementDef("impl_b", "revenue_b", "bad_b.sql"),
        ],
    )
    from app.core.registry import MetricRegistry

    MetricRegistry(registry_conn, (fixture_tree / "impls", impls_dir)).import_definition(
        metric, "<inline>"
    )
    catalog = _open_catalog(fixture_tree)
    try:
        # 与一个正常指标同批执行，证明单条归因失败不拖垮整批 run
        run_id = perform_run(
            registry_conn,
            ["bad_alignment", "active_users"],
            catalog,
            (fixture_tree / "impls", impls_dir),
            datasets_dir=fixture_tree / "datasets",
            abs_tolerance=1e-6,
            rel_tolerance=0.0,
        )
    finally:
        catalog.close()

    snapshots = {item["metric_key"]: item for item in snapshots_for_run(registry_conn, run_id)}
    assert set(snapshots) == {"bad_alignment", "active_users"}
    bad = snapshots["bad_alignment"]["result"]["attribution"]
    assert bad["status"] == "degraded"
    assert bad["degraded_reasons"][0]["code"] == "natural_key_column_missing"
    # 维度差值仍然保留
    assert snapshots["bad_alignment"]["result"]["dimensions"]
    good = snapshots["active_users"]["result"]["attribution"]
    assert good["status"] == "skipped"

    from app.core.snapshot import get_run

    summary = get_run(registry_conn, run_id)["summary"]
    assert summary["attribution"] == {"ok": 0, "degraded": 1, "skipped": 1}


def test_legacy_snapshot_without_attribution_is_flagged_in_report(
    registry_conn, fixture_tree: Path, sql_roots
):
    catalog = _open_catalog(fixture_tree)
    try:
        run_id = perform_run(
            registry_conn,
            ["active_users"],
            catalog,
            sql_roots,
            datasets_dir=fixture_tree / "datasets",
            abs_tolerance=1e-6,
            rel_tolerance=0.0,
        )
    finally:
        catalog.close()

    # 模拟老快照：抹掉 result_json 里的 attribution 字段
    snapshot = snapshots_for_run(registry_conn, run_id)[0]
    result = snapshot["result"]
    result.pop("attribution", None)
    registry_conn.execute(
        "UPDATE snapshots SET result_json = ? WHERE id = ?",
        (json.dumps(result, ensure_ascii=False, sort_keys=True), snapshot["id"]),
    )
    registry_conn.commit()

    report = build_report(registry_conn, run_id)
    item = report["metrics"][0]
    assert item["attribution"]["status"] == "unsupported"
    assert item["attribution"]["degraded_reasons"][0]["code"] == "legacy_snapshot"

    # 三种报告格式都能识别老快照且不报错
    _, raw_json = export_report(report, "json")
    assert "legacy_snapshot" in raw_json
    _, markdown = export_report(report, "md")
    assert "unsupported" in markdown
    _, html = export_report(report, "html")
    assert "unsupported" in html


def test_declared_natural_key_metric_renders_clusters_in_report(
    registry_conn, fixture_tree: Path, sql_roots
):
    catalog = _open_catalog(fixture_tree)
    try:
        run_id = perform_run(
            registry_conn,
            ["segment_revenue"],
            catalog,
            sql_roots,
            datasets_dir=fixture_tree / "datasets",
            abs_tolerance=1e-6,
            rel_tolerance=0.0,
        )
    finally:
        catalog.close()
    report = build_report(registry_conn, run_id)
    item = report["metrics"][0]
    assert report["summary"]["attribution"]["ok"] == 1
    _, markdown = export_report(report, "md")
    assert "明细差异聚类归因" in markdown
    assert "only_one_side" in markdown
    assert "type_mismatch" in markdown
    assert "value_deviation" in markdown
    _, html = export_report(report, "html")
    assert "明细差异聚类归因" in html
    assert "region, segment" in html


def test_dsl_natural_keys_round_trip(tmp_path: Path):
    text = """
key: daily_orders
name: 每日订单数
owner: data-platform
description: 测试
unit: 单
grain: day
natural_keys:
  - region
  - segment
impls:
  - id: impl_a
    source: orders_a
    sql_file: a.sql
  - id: impl_b
    source: orders_b
    sql_file: b.sql
"""
    metric = parse_metric_text(text)
    assert metric.natural_keys == ["region", "segment"]
    # 序列化进定义、再从字典读回（模拟快照/版本仓库路径）
    restored = MetricDefinition.from_dict(metric.to_dict())
    assert restored.natural_keys == ["region", "segment"]

    # 字符串简写也支持
    shorthand = parse_metric_text(text.replace(
        "natural_keys:\n  - region\n  - segment", "natural_keys: region"
    ))
    assert shorthand.natural_keys == ["region"]


def test_attribution_failure_inside_run_marks_degraded_not_failed(
    registry_conn, fixture_tree: Path, sql_roots, monkeypatch
):
    from app.core import evaluator

    def boom(*args, **kwargs):
        raise RuntimeError("simulated attribution crash")

    monkeypatch.setattr(evaluator, "attribute_differences", boom)
    catalog = _open_catalog(fixture_tree)
    try:
        run_id = perform_run(
            registry_conn,
            ["segment_revenue"],
            catalog,
            sql_roots,
            datasets_dir=fixture_tree / "datasets",
            abs_tolerance=1e-6,
            rel_tolerance=0.0,
        )
    finally:
        catalog.close()
    snapshot = snapshots_for_run(registry_conn, run_id)[0]
    attribution = snapshot["result"]["attribution"]
    assert attribution["status"] == "degraded"
    assert attribution["degraded_reasons"][0]["code"] == "attribution_error"
    # run 本身仍然完成
    from app.core.snapshot import get_run

    assert get_run(registry_conn, run_id)["status"] == "completed"
