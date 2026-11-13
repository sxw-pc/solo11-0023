"""多维下钻与失败归因的验收测试。

覆盖：多规则多失败簇的聚合、簇划分稳定性、代表值可追溯性、
老版本结果产物（无失败样例字段）的降级路径、分块流式处理。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.comparator import compare_impl_results
from app.core.drilldown import (
    DrilldownAggregator,
    build_drilldown,
    build_drilldown_from_file,
    iter_snapshot_events,
)
from app.core.reporter import build_report, render_json
from app.core.snapshot import create_run, finish_run, save_snapshot, snapshots_for_run
from app.db import init_db
from app.models import ImplEvaluation, ImplementDef, MetricDefinition
from cli.main import main


def _metric(key: str, sources: tuple[str, ...] = ("orders_a", "orders_b")) -> MetricDefinition:
    impl_ids = ("impl_a", "impl_b")[: len(sources)]
    return MetricDefinition(
        key=key,
        name=key,
        owner="test",
        description="",
        unit="",
        grain="day",
        impls=[
            ImplementDef(id=impl_id, source=source, sql_file=f"{key}_{impl_id}.sql")
            for impl_id, source in zip(impl_ids, sources)
        ],
    )


def _save(conn, run_id: int, metric: MetricDefinition, values_by_impl: dict) -> int:
    comparison = compare_impl_results(values_by_impl, metric_key=metric.key, metric_version=1)
    evaluations = {
        impl.id: ImplEvaluation(
            impl_id=impl.id,
            source=impl.source,
            sql_file=impl.sql_file,
            values=values_by_impl[impl.id],
            row_count=len(values_by_impl[impl.id]),
            duration_ms=1.0,
        )
        for impl in metric.impls
    }
    return save_snapshot(conn, run_id, metric, comparison, evaluations)


def _new_run(conn, label: str) -> int:
    run_id = create_run(conn, [label], "datasets", {"abs": 1e-6, "rel": 0.0})
    finish_run(conn, run_id, status="completed", summary={})
    return run_id


def _orders_values() -> dict:
    return {
        "impl_a": {"2026-11-01": 10.0, "2026-11-02": 11.0, "2026-11-03": 12.0, "2026-11-04": 5.0},
        "impl_b": {"2026-11-01": 20.0, "2026-11-02": 21.0, "2026-11-03": 22.0, "2026-11-04": 5.0},
    }


def _revenue_values() -> dict:
    return {
        "impl_a": {"2026-11-01": 100.0, "2026-11-02": 50.0},
        "impl_b": {"2026-11-01": 100.001, "2026-11-02": 50.0},
    }


@pytest.fixture
def two_batch_repo(conn):
    """两个批次、三条规则、三个失败簇的执行结果仓库。"""
    orders = _metric("orders_consistency")
    users = _metric("users_completeness", sources=("users_a", "users_b"))
    revenue = _metric("revenue_consistency")

    run1 = _new_run(conn, "batch-1")
    _save(conn, run1, orders, _orders_values())
    _save(conn, run1, users, {
        "impl_a": {"2026-11-01": 7.0, "2026-11-02": 8.0, "2026-11-03": 9.0},
        "impl_b": {"2026-11-03": 9.0},
    })
    _save(conn, run1, revenue, _revenue_values())

    run2 = _new_run(conn, "batch-2")
    _save(conn, run2, orders, _orders_values())
    _save(conn, run2, users, {
        "impl_a": {"2026-11-01": 7.0, "2026-11-02": 8.0, "2026-11-03": 9.0},
        "impl_b": {"2026-11-01": 7.0, "2026-11-02": 8.0, "2026-11-03": 9.0},
    })
    _save(conn, run2, revenue, _revenue_values())
    return conn, (run1, run2)


def _strip_failures(conn, snapshot_ids: set[int] | None = None) -> None:
    """把结果回退为老版本产物（移除 failures 失败样例字段）。"""
    rows = conn.execute("SELECT id, result_json FROM snapshots").fetchall()
    for row in rows:
        if snapshot_ids is not None and row["id"] not in snapshot_ids:
            continue
        result = json.loads(row["result_json"])
        result.pop("failures", None)
        conn.execute(
            "UPDATE snapshots SET result_json = ? WHERE id = ?",
            (json.dumps(result, ensure_ascii=False, sort_keys=True), row["id"]),
        )
    conn.commit()


def _without_generated_at(report: dict) -> dict:
    report = dict(report)
    report.pop("generated_at", None)
    return report


# ---------------------------------------------------------------------------
# 多规则多簇：划分、聚合、归因
# ---------------------------------------------------------------------------


def test_clusters_are_stable_and_attributed(two_batch_repo):
    conn, (run1, run2) = two_batch_repo
    first = _without_generated_at(build_drilldown(conn))
    second = _without_generated_at(build_drilldown(conn))
    assert first == second, "同一仓库两次聚合必须完全一致（稳定可复现）"

    assert first["mode"] == "full"
    assert first["degraded"] is False
    assert first["summary"]["total_failures"] == 10
    assert first["summary"]["dimensions_checked"] == 18

    clusters = {c["signature"]["delta_bucket"]: c for c in first["clusters"]}
    assert set(clusters) == {"1e1", "na", "1e-3"}

    big = clusters["1e1"]
    assert big["failure_count"] == 6
    assert big["first_batch"] == run1
    assert big["last_batch"] == run2
    assert big["affected_ratio"] == round(6 / 18, 6)
    assert big["fields"] == ["orders_consistency"]
    assert "字段级脏数据" in big["hint"]

    missing = clusters["na"]
    assert missing["failure_count"] == 2
    assert missing["signature"]["datasets"] == ["users_b"]
    assert missing["first_batch"] == run1 == missing["last_batch"]
    assert "数据缺失" in missing["hint"]

    small = clusters["1e-3"]
    assert small["failure_count"] == 2
    assert small["fields"] == ["revenue_consistency"]


def test_dimension_rollups(two_batch_repo):
    conn, (run1, _) = two_batch_repo
    report = build_drilldown(conn)

    by_dataset = {item["dataset"]: item for item in report["by_dataset"]}
    assert by_dataset["orders_a"]["failures"] == 8
    assert by_dataset["orders_b"]["failures"] == 8
    assert by_dataset["users_b"]["failures"] == 2
    assert by_dataset["users_b"]["first_batch"] == run1

    by_field = {item["field"]: item for item in report["by_field"]}
    assert by_field["orders_consistency"]["failures"] == 6
    assert by_field["revenue_consistency"]["failures"] == 2
    assert by_field["users_completeness"]["failures"] == 2

    by_type = {item["rule_type"]: item for item in report["by_rule_type"]}
    assert by_type["value_mismatch"]["failures"] == 8
    assert by_type["missing_dimension"]["failures"] == 2

    by_rule = {item["rule"]: item for item in report["by_rule"]}
    assert by_rule["orders_consistency"]["dimensions_checked"] == 8
    assert by_rule["orders_consistency"]["failures"] == 6
    assert by_rule["orders_consistency"]["affected_ratio"] == round(6 / 8, 6)

    missing_values = [v for v in report["by_sample_value"] if v["value"] is None]
    assert missing_values and missing_values[0]["count"] == 2


def test_representatives_come_from_raw_failures(two_batch_repo):
    conn, (run1, run2) = two_batch_repo
    report = build_drilldown(conn)

    raw_values: set[str] = set()
    for run_id in (run1, run2):
        for snapshot in snapshots_for_run(conn, run_id):
            for failure in snapshot["result"]["failures"]:
                raw_values.add(
                    json.dumps(
                        {s["impl_id"]: s["value"] for s in failure["samples"]},
                        sort_keys=True,
                    )
                )
    representatives = [
        rep for cluster in report["clusters"] for rep in cluster["representatives"]
    ]
    assert representatives, "失败簇必须给出代表性失败值"
    for rep in representatives:
        assert json.dumps(rep["values"], sort_keys=True) in raw_values, (
            "代表性失败值必须能在原始失败明细中找到，不允许脱离明细构造"
        )
    big = max(report["clusters"], key=lambda c: c["failure_count"])
    assert len(big["representatives"]) == 3  # 两批次重复值去重后剩 3 组


def test_batch_anomaly_hint_for_multi_field_single_batch(conn):
    run_id = _new_run(conn, "batch-only")
    _save(conn, run_id, _metric("metric_x"), {"impl_a": {"d1": 1.0}, "impl_b": {"d1": 9.0}})
    _save(conn, run_id, _metric("metric_y"), {"impl_a": {"d1": 2.0}, "impl_b": {"d1": 8.0}})
    report = build_drilldown(conn)
    assert len(report["clusters"]) == 1
    cluster = report["clusters"][0]
    assert cluster["fields"] == ["metric_x", "metric_y"]
    assert cluster["batch_count"] == 1
    assert "整批采集异常" in cluster["hint"]


# ---------------------------------------------------------------------------
# 老版本产物降级
# ---------------------------------------------------------------------------


def test_legacy_results_degrade_to_rule_summary(two_batch_repo):
    conn, _ = two_batch_repo
    _strip_failures(conn)
    report = build_drilldown(conn)  # 不允许报错
    assert report["mode"] == "legacy"
    assert report["degraded"] is True
    assert any("降级" in notice for notice in report["notices"])
    assert report["clusters"] == []
    assert report["by_dataset"] == []
    rules = {item["rule"]: item for item in report["by_rule"]}
    assert rules["orders_consistency"]["failures"] == 0
    assert rules["orders_consistency"]["legacy_diffs"] == 6
    assert rules["orders_consistency"]["dimensions_checked"] == 8
    assert rules["users_completeness"]["legacy_diffs"] == 2


def test_mixed_modern_and_legacy_is_partial(two_batch_repo):
    conn, _ = two_batch_repo
    first_id = conn.execute("SELECT MIN(id) FROM snapshots").fetchone()[0]
    _strip_failures(conn, {first_id})
    report = build_drilldown(conn)
    assert report["mode"] == "partial"
    assert report["degraded"] is True
    assert any("旧版本" in notice for notice in report["notices"])
    assert report["summary"]["legacy_results"] == 1
    assert report["summary"]["modern_results"] == 5
    assert report["clusters"], "新版快照仍然参与聚簇"


# ---------------------------------------------------------------------------
# 分块与流式
# ---------------------------------------------------------------------------


def test_chunk_size_does_not_change_results(two_batch_repo):
    conn, _ = two_batch_repo
    small = _without_generated_at(build_drilldown(conn, chunk_size=1))
    large = _without_generated_at(build_drilldown(conn, chunk_size=1000))
    small["scope"].pop("chunk_size")
    large["scope"].pop("chunk_size")
    assert small == large


def test_snapshot_events_stream_in_order_without_materializing(two_batch_repo):
    conn, _ = two_batch_repo
    events = iter_snapshot_events(conn, chunk_size=2)
    assert iter(events) is events, "必须是惰性迭代器而不是物化列表"
    seen = [event["snapshot_id"] for event in events]
    assert len(seen) == 6
    assert seen == sorted(seen), "keyset 分块必须按快照 id 顺序产出"


def test_aggregator_accepts_one_record_at_a_time(two_batch_repo):
    conn, _ = two_batch_repo
    aggregator = DrilldownAggregator()
    for event in iter_snapshot_events(conn, chunk_size=3):
        aggregator.add_event(event)
    streamed = _without_generated_at(aggregator.finish(scope={"source": "test"}))
    direct = _without_generated_at(build_drilldown(conn))
    streamed_summary, direct_summary = streamed["summary"], direct["summary"]
    assert streamed_summary == direct_summary


# ---------------------------------------------------------------------------
# 结果文件输入（含老版本导出文件降级）
# ---------------------------------------------------------------------------


def test_report_file_mode_matches_repository_mode(two_batch_repo, tmp_path):
    conn, (run1, _) = two_batch_repo
    path = tmp_path / "report.json"
    path.write_text(render_json(build_report(conn, run1)), encoding="utf-8")
    from_file = build_drilldown_from_file(path)
    from_db = build_drilldown(conn, run_id=run1)
    left, right = _without_generated_at(from_file), _without_generated_at(from_db)
    left.pop("scope")
    right.pop("scope")
    assert left == right


def test_legacy_report_file_degrades_with_notice(two_batch_repo, tmp_path):
    conn, (run1, _) = two_batch_repo
    doc = json.loads(render_json(build_report(conn, run1)))
    for metric in doc["metrics"]:
        metric.pop("failures", None)  # 老版本导出的报告没有失败样例字段
    path = tmp_path / "legacy-report.json"
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    report = build_drilldown_from_file(path)  # 不允许报错
    assert report["mode"] == "legacy"
    assert report["degraded"] is True
    assert any("降级" in notice for notice in report["notices"])
    assert report["clusters"] == []
    assert {item["rule"] for item in report["by_rule"]} == {
        "orders_consistency",
        "users_completeness",
        "revenue_consistency",
    }


def test_jsonl_file_streams_records(two_batch_repo, tmp_path):
    conn, (run1, _) = two_batch_repo
    failures = [
        failure
        for snapshot in snapshots_for_run(conn, run1)
        for failure in snapshot["result"]["failures"]
    ]
    path = tmp_path / "failures.jsonl"
    path.write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in failures),
        encoding="utf-8",
    )
    report = build_drilldown_from_file(path)
    assert report["summary"]["total_failures"] == len(failures) == 6
    assert len(report["clusters"]) == 3
    assert report["clusters"][0]["affected_ratio"] is None  # JSONL 无校验总量
    assert any("JSONL" in notice for notice in report["notices"])


def test_unreadable_result_file_raises_clear_error(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text('{"hello": "world"}', encoding="utf-8")
    with pytest.raises(Exception, match="无法识别的结果文件格式"):
        build_drilldown_from_file(path)


# ---------------------------------------------------------------------------
# CLI 与 API
# ---------------------------------------------------------------------------


def _cli_prefix(db_path: Path, fixture_tree: Path) -> list[str]:
    return ["--db", str(db_path), "--fixtures-dir", str(fixture_tree)]


def test_cli_drilldown_after_real_run(tmp_path, fixture_tree, capsys):
    db_path = tmp_path / "cli.db"
    prefix = _cli_prefix(db_path, fixture_tree)
    assert main(prefix + ["import-metrics", "--dir", str(fixture_tree / "metrics")]) == 0
    assert main(prefix + ["run", "--datasets", str(fixture_tree / "datasets")]) == 0
    capsys.readouterr()

    assert main(prefix + ["drilldown", "--format", "json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["mode"] == "full"
    assert doc["clusters"], "真实执行应产生可聚簇的失败明细"
    rules = {item["rule"] for item in doc["by_rule"]}
    assert {"active_users", "conversion_rate", "avg_order_value"} <= rules
    failing = {item["rule"] for item in doc["by_rule"] if item["failures"] > 0}
    assert failing == {"avg_order_value"}


def test_cli_drilldown_legacy_file_falls_back(tmp_path, fixture_tree, capsys):
    db_path = tmp_path / "cli.db"
    prefix = _cli_prefix(db_path, fixture_tree)
    assert main(prefix + ["import-metrics", "--dir", str(fixture_tree / "metrics")]) == 0
    assert main(prefix + ["run", "--datasets", str(fixture_tree / "datasets")]) == 0
    report_path = tmp_path / "report.json"
    assert main(prefix + ["report", "--last", "--format", "json", "--output", str(report_path)]) == 0

    doc = json.loads(report_path.read_text(encoding="utf-8"))
    for metric in doc["metrics"]:
        metric.pop("failures", None)
    legacy_path = tmp_path / "legacy.json"
    legacy_path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    capsys.readouterr()  # 排空前面命令的输出

    assert main(prefix + ["drilldown", "--file", str(legacy_path), "--format", "json"]) == 0
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["mode"] == "legacy"
    assert result["degraded"] is True
    assert "降级" in captured.err, "降级路径必须给出明确提示"


def test_api_drilldown_endpoint(app, app_config, client):
    conn = init_db(app_config.db_path)
    try:
        run_id = _new_run(conn, "api-batch")
        _save(conn, run_id, _metric("metric_x"), {"impl_a": {"d1": 1.0}, "impl_b": {"d1": 9.0}})
    finally:
        conn.close()
    response = client.get("/api/drilldown/")
    assert response.status_code == 200
    doc = response.get_json()
    assert doc["summary"]["total_failures"] == 1
    assert doc["clusters"][0]["first_batch"] == run_id

    scoped = client.get(f"/api/drilldown/?run_id={run_id + 99}")
    assert scoped.status_code == 200
    assert scoped.get_json()["summary"]["total_failures"] == 0
