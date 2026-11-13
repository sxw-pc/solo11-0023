"""Batch baseline comparison: alignment, degradation, stability and switch-off."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.baseline import (
    REASON_BASELINE_METRIC_MISSING,
    REASON_BASELINE_NOT_COMPLETED,
    REASON_BASELINE_NOT_FOUND,
    REASON_IMPL_SET_CHANGED,
    REASON_VERSION_MISMATCH,
    REASON_NO_MATCHING_BASELINE,
    STRATEGY,
)
from app.core.dataset_loader import DatasetCatalog
from app.core.reporter import build_report, export_report
from app.core.snapshot import get_run, perform_run, snapshots_for_run
from app.core.versioning import append_version, get_latest
from app.errors import NotFoundError
from app.models import ImplementDef, MetricDefinition


def _run(conn, catalog, sql_roots, datasets_dir, keys=None,
         abs_tolerance=1e-6, rel_tolerance=0.0, **kwargs):
    return perform_run(
        conn,
        keys,
        catalog,
        sql_roots,
        datasets_dir=datasets_dir,
        timeout_seconds=2.0,
        max_rows=100,
        abs_tolerance=abs_tolerance,
        rel_tolerance=rel_tolerance,
        **kwargs,
    )


@pytest.fixture
def catalog_and_dir(fixture_tree: Path):
    datasets_dir = fixture_tree / "datasets"
    catalog = DatasetCatalog(datasets_dir)
    try:
        yield catalog, datasets_dir
    finally:
        catalog.close()


# ---------------------------------------------------------------------------
# 1. 正常基线 run：自动选取、显式锚定、verdict/dimension 对照
# ---------------------------------------------------------------------------

def test_first_run_without_baseline_degrades_gracefully(
    registry_conn, catalog_and_dir, fixture_tree, sql_roots
):
    catalog, datasets_dir = catalog_and_dir
    run_id = _run(registry_conn, catalog, sql_roots, datasets_dir)
    comparison = get_run(registry_conn, run_id)["summary"]["baseline_comparison"]
    assert comparison["enabled"] is True
    assert comparison["status"] == "degraded"
    assert comparison["degrade_reason"] == REASON_NO_MATCHING_BASELINE
    assert comparison["strategy"] == STRATEGY
    assert comparison["metrics"] == {}
    assert "降级" in comparison["summary_text"]


def test_second_run_aligns_against_completed_baseline(
    registry_conn, catalog_and_dir, fixture_tree, sql_roots
):
    catalog, datasets_dir = catalog_and_dir
    first_id = _run(registry_conn, catalog, sql_roots, datasets_dir,
                    keys=["active_users", "avg_order_value"])
    second_id = _run(registry_conn, catalog, sql_roots, datasets_dir,
                     keys=["active_users", "avg_order_value"])
    comparison = get_run(registry_conn, second_id)["summary"]["baseline_comparison"]

    assert comparison["status"] == "ok"
    assert comparison["baseline_selection"] == "auto"
    assert comparison["baseline_run_id"] == first_id
    assert comparison["comparable_count"] == 2
    assert comparison["incomparable_count"] == 0
    for key, block in comparison["metrics"].items():
        assert block["comparable"] is True
        assert block["reasons"] == []
        assert block["baseline_verdict"] == block["current_verdict"]
        assert block["verdict_change"] == "unchanged"
        changes = block["dimension_changes"]
        assert changes["dimensions_added"] == []
        assert changes["dimensions_removed"] == []


def test_explicit_baseline_run_id_is_anchored(
    registry_conn, catalog_and_dir, fixture_tree, sql_roots
):
    catalog, datasets_dir = catalog_and_dir
    first_id = _run(registry_conn, catalog, sql_roots, datasets_dir,
                    keys=["active_users"])
    # 中间插入一个不同指标集合的 run，自动选取会被它干扰，显式 id 不受影响
    _run(registry_conn, catalog, sql_roots, datasets_dir,
         keys=["conversion_rate"])
    third_id = _run(
        registry_conn, catalog, sql_roots, datasets_dir,
        keys=["active_users"], baseline_run_id=first_id,
    )
    comparison = get_run(registry_conn, third_id)["summary"]["baseline_comparison"]
    assert comparison["baseline_selection"] == "explicit"
    assert comparison["baseline_run_id"] == first_id
    assert comparison["status"] == "ok"


def test_verdict_regression_and_dimension_changes_are_detected(
    registry_conn, catalog_and_dir, fixture_tree, sql_roots
):
    catalog, datasets_dir = catalog_and_dir
    first_id = _run(registry_conn, catalog, sql_roots, datasets_dir,
                    keys=["active_users"])
    baseline_block_snapshot = snapshots_for_run(registry_conn, first_id)[0]
    assert baseline_block_snapshot["verdict"] == "consistent"

    users_b = fixture_tree / "datasets" / "users_b.csv"
    users_b.write_text(
        users_b.read_text(encoding="utf-8") + "2008,2026-01-20,2026-02-01,active\n",
        encoding="utf-8",
    )
    # CSV 在 catalog 创建时载入内存，改文件后要重新打开（与既有漂移测试一致）
    catalog.close()
    catalog = DatasetCatalog(datasets_dir)
    second_id = _run(registry_conn, catalog, sql_roots, datasets_dir,
                     keys=["active_users"])
    comparison = get_run(registry_conn, second_id)["summary"]["baseline_comparison"]
    block = comparison["metrics"]["active_users"]

    assert block["comparable"] is True
    assert block["baseline_verdict"] == "consistent"
    assert block["current_verdict"] == "inconsistent"
    assert block["verdict_change"] == "regressed"
    assert block["dimension_changes"]["diff_dimensions_added"]
    assert comparison["summary_text"].startswith(f"基线 run #{first_id}")


def test_verdict_improvement_is_classified(
    registry_conn, catalog_and_dir, fixture_tree, sql_roots
):
    catalog, datasets_dir = catalog_and_dir
    # avg_order_value 在 fixture 下天然 inconsistent
    bad_id = _run(registry_conn, catalog, sql_roots, datasets_dir,
                  keys=["avg_order_value"], abs_tolerance=1e-6)
    assert snapshots_for_run(registry_conn, bad_id)[0]["verdict"] == "inconsistent"
    # 放大容差后转为 consistent，对照基线应判为 improved
    good_id = _run(
        registry_conn, catalog, sql_roots, datasets_dir,
        keys=["avg_order_value"], baseline_run_id=bad_id, abs_tolerance=1000.0,
    )
    block = get_run(registry_conn, good_id)["summary"]["baseline_comparison"]
    metric_block = block["metrics"]["avg_order_value"]
    assert metric_block["comparable"] is True
    assert metric_block["baseline_verdict"] == "inconsistent"
    assert metric_block["current_verdict"] == "consistent"
    assert metric_block["verdict_change"] == "improved"


def test_same_baseline_repeated_comparison_is_stable(
    registry_conn, catalog_and_dir, fixture_tree, sql_roots
):
    catalog, datasets_dir = catalog_and_dir
    baseline_id = _run(registry_conn, catalog, sql_roots, datasets_dir,
                       keys=["active_users", "conversion_rate"])
    run_a = _run(registry_conn, catalog, sql_roots, datasets_dir,
                 keys=["active_users", "conversion_rate"],
                 baseline_run_id=baseline_id)
    run_b = _run(registry_conn, catalog, sql_roots, datasets_dir,
                 keys=["active_users", "conversion_rate"],
                 baseline_run_id=baseline_id)
    block_a = get_run(registry_conn, run_a)["summary"]["baseline_comparison"]
    block_b = get_run(registry_conn, run_b)["summary"]["baseline_comparison"]
    assert block_a["status"] == block_b["status"] == "ok"
    assert block_a["baseline_run_id"] == block_b["baseline_run_id"] == baseline_id
    # 同一锚点、同一批指标定义，逐指标结论必须稳定一致
    assert block_a["metrics"] == block_b["metrics"]


# ---------------------------------------------------------------------------
# 2. 新增 / 删除指标：基线缺失指标被显式标记，其余指标正常对照
# ---------------------------------------------------------------------------

def test_added_and_removed_metrics_are_marked_not_rejected(
    registry_conn, catalog_and_dir, fixture_tree, sql_roots
):
    catalog, datasets_dir = catalog_and_dir
    # 基线跑两个指标
    _run(registry_conn, catalog, sql_roots, datasets_dir,
         keys=["active_users", "conversion_rate"])
    # 本次新增 avg_order_value，删除 conversion_rate；自动选取找不到同集合 run
    current_id = _run(
        registry_conn, catalog, sql_roots, datasets_dir,
        keys=["active_users", "avg_order_value"],
        # 显式锚定基线，强制走逐指标对齐而不是整批降级
        baseline_run_id=1,
    )
    comparison = get_run(registry_conn, current_id)["summary"]["baseline_comparison"]
    assert comparison["status"] == "ok"
    assert comparison["incomparable_count"] == 1
    assert comparison["comparable_count"] == 1

    added = comparison["metrics"]["avg_order_value"]
    assert added["comparable"] is False
    assert REASON_BASELINE_METRIC_MISSING in added["reasons"]
    assert "baseline_verdict" not in added
    assert added["current_verdict"]

    active = comparison["metrics"]["active_users"]
    assert active["comparable"] is True
    # 摘要里要能读到缺失定位
    assert "avg_order_value" in comparison["summary_text"]
    assert "基线缺少该指标" in comparison["summary_text"]


def test_auto_selection_skips_runs_with_different_metric_sets(
    registry_conn, catalog_and_dir, fixture_tree, sql_roots
):
    catalog, datasets_dir = catalog_and_dir
    _run(registry_conn, catalog, sql_roots, datasets_dir,
         keys=["active_users"])
    current_id = _run(registry_conn, catalog, sql_roots, datasets_dir,
                      keys=["active_users", "conversion_rate"])
    comparison = get_run(registry_conn, current_id)["summary"]["baseline_comparison"]
    assert comparison["status"] == "degraded"
    assert comparison["degrade_reason"] == REASON_NO_MATCHING_BASELINE
    # 本批快照仍正常落库，未被拒绝
    assert len(snapshots_for_run(registry_conn, current_id)) == 2


# ---------------------------------------------------------------------------
# 3. impl 集合缩减与 metric 版本不一致：显式不可比
# ---------------------------------------------------------------------------

def test_impl_set_shrinkage_marks_metric_incomparable(
    registry_conn, catalog_and_dir, fixture_tree, sql_roots
):
    catalog, datasets_dir = catalog_and_dir
    baseline_id = _run(registry_conn, catalog, sql_roots, datasets_dir,
                       keys=["active_users"])
    before = snapshots_for_run(registry_conn, baseline_id)[0]
    assert set(before["impl_ids"]) == {"impl_a", "impl_b"}

    # 重新导入 v2：impl 集合从两个缩减为一个（复用现有只读 SQL）
    latest = get_latest(registry_conn, "active_users")
    definition = MetricDefinition.from_dict(latest["definition"])
    shrunk = MetricDefinition(
        key=definition.key,
        name=definition.name,
        owner=definition.owner,
        description=definition.description,
        unit=definition.unit,
        grain=definition.grain,
        impls=[
            ImplementDef(id="impl_a", source="users_a",
                         sql_file="active_users_impl_a.sql"),
            ImplementDef(id="impl_c", source="users_b",
                         sql_file="active_users_impl_b.sql"),
        ],
    )
    new_version = append_version(registry_conn, shrunk, "<inline-test>")
    assert new_version == latest["version"] + 1

    current_id = _run(registry_conn, catalog, sql_roots, datasets_dir,
                      keys=["active_users"], baseline_run_id=baseline_id)
    comparison = get_run(registry_conn, current_id)["summary"]["baseline_comparison"]
    block = comparison["metrics"]["active_users"]
    assert block["comparable"] is False
    assert REASON_IMPL_SET_CHANGED in block["reasons"]
    assert block["impls_added"] == ["impl_c"]
    assert block["impls_removed"] == ["impl_b"]
    assert block["baseline_version"] != block["current_version"]
    assert "实现集合变化" in comparison["summary_text"]
    # 快照结构未变，仍正常记录本次 impl 集合
    current_snapshot = snapshots_for_run(registry_conn, current_id)[0]
    assert set(current_snapshot["impl_ids"]) == {"impl_a", "impl_c"}


def test_metric_version_mismatch_alone_is_never_silently_aligned(
    registry_conn, catalog_and_dir, fixture_tree, sql_roots
):
    catalog, datasets_dir = catalog_and_dir
    baseline_id = _run(registry_conn, catalog, sql_roots, datasets_dir,
                       keys=["active_users"])
    # 同 impl 集合、同 SQL，仅定义版本号推进（描述变化）
    latest = get_latest(registry_conn, "active_users")
    definition = MetricDefinition.from_dict(latest["definition"])
    definition.description = definition.description + "（口径描述更新）"
    append_version(registry_conn, definition, "<inline-test>")

    current_id = _run(registry_conn, catalog, sql_roots, datasets_dir,
                      keys=["active_users"], baseline_run_id=baseline_id)
    block = get_run(registry_conn, current_id)["summary"]["baseline_comparison"]
    metric_block = block["metrics"]["active_users"]
    assert metric_block["comparable"] is False
    assert REASON_VERSION_MISMATCH in metric_block["reasons"]
    assert REASON_IMPL_SET_CHANGED not in metric_block["reasons"]


# ---------------------------------------------------------------------------
# 4. 失败 / 半成品基线降级
# ---------------------------------------------------------------------------

def test_failed_or_running_baseline_degrades_with_reason(
    registry_conn, catalog_and_dir, fixture_tree, sql_roots
):
    catalog, datasets_dir = catalog_and_dir
    # 手工伪造一个 failed 半成品 run（含部分快照）
    from app.core.snapshot import create_run, finish_run

    failed_id = create_run(
        registry_conn, ["active_users"], datasets_dir, {"abs": 1e-6, "rel": 0.0}
    )
    finish_run(registry_conn, failed_id, status="failed", error="boom")
    current_id = _run(
        registry_conn, catalog, sql_roots, datasets_dir,
        keys=["active_users"], baseline_run_id=failed_id,
    )
    comparison = get_run(registry_conn, current_id)["summary"]["baseline_comparison"]
    assert comparison["status"] == "degraded"
    assert comparison["degrade_reason"] == REASON_BASELINE_NOT_COMPLETED
    assert comparison["degrade_detail"]["baseline_status"] == "failed"
    assert len(snapshots_for_run(registry_conn, current_id)) == 1

    # 不存在的基线 id：另一种降级原因，同样不拒绝整批
    other_id = _run(
        registry_conn, catalog, sql_roots, datasets_dir,
        keys=["active_users"], baseline_run_id=99999,
    )
    other = get_run(registry_conn, other_id)["summary"]["baseline_comparison"]
    assert other["degrade_reason"] == REASON_BASELINE_NOT_FOUND


def test_current_run_failure_records_degraded_anchor(
    registry_conn, catalog_and_dir, fixture_tree, sql_roots, monkeypatch
):
    catalog, datasets_dir = catalog_and_dir
    baseline_id = _run(registry_conn, catalog, sql_roots, datasets_dir,
                       keys=["active_users"])

    def boom(*args, **kwargs):
        raise RuntimeError("evaluation exploded")

    monkeypatch.setattr("app.core.snapshot.run_metric", boom)
    with pytest.raises(Exception):
        _run(registry_conn, catalog, sql_roots, datasets_dir,
            keys=["active_users"], baseline_run_id=baseline_id)

    failed_runs = [
        row for row in registry_conn.execute(
            "SELECT id, status, summary_json FROM runs WHERE status = 'failed'"
        ).fetchall()
    ]
    assert failed_runs
    summary = json.loads(failed_runs[-1]["summary_json"])
    anchor = summary["baseline_comparison"]
    assert anchor["status"] == "degraded"
    assert anchor["degrade_reason"] == "current_run_failed"
    assert anchor["baseline_run_id"] == baseline_id


def test_unknown_metric_still_produces_failed_run_row(
    registry_conn, catalog_and_dir, fixture_tree, sql_roots
):
    """预检对缺失指标容错；规范错误仍由执行循环抛出并落 failed run。"""
    catalog, datasets_dir = catalog_and_dir
    with pytest.raises(NotFoundError):
        _run(registry_conn, catalog, sql_roots, datasets_dir,
             keys=["missing_metric"])
    row = registry_conn.execute(
        "SELECT status, error FROM runs WHERE status = 'failed'"
    ).fetchone()
    assert row is not None
    assert "missing_metric" in row[1]
    actions = [r[0] for r in registry_conn.execute(
        "SELECT action FROM audit_log WHERE action = 'run.failed'")]
    assert actions == ["run.failed"]


# ---------------------------------------------------------------------------
# 5. 关闭对照开关：与老行为完全一致
# ---------------------------------------------------------------------------

def test_disabled_comparison_keeps_legacy_summary_shape(
    registry_conn, catalog_and_dir, fixture_tree, sql_roots
):
    catalog, datasets_dir = catalog_and_dir
    _run(registry_conn, catalog, sql_roots, datasets_dir,
         keys=["active_users"])
    legacy_id = _run(
        registry_conn, catalog, sql_roots, datasets_dir,
        keys=["active_users"], compare_baseline=False,
    )
    summary = get_run(registry_conn, legacy_id)["summary"]
    assert "baseline_comparison" not in summary
    assert set(summary) == {"consistent", "inconsistent", "partial", "metrics", "run_id"}
    # 快照表结构与内容不受影响
    snapshots = snapshots_for_run(registry_conn, legacy_id)
    assert snapshots[0]["verdict"] == "consistent"
    assert set(snapshots[0]["impl_ids"]) == {"impl_a", "impl_b"}

    # 报告里不出现基线段落
    report = build_report(registry_conn, legacy_id)
    _, markdown = export_report(report, "md")
    assert "批次基线对照" not in markdown
    _, html = export_report(report, "html")
    assert "批次基线对照" not in html

    # 审计层面也不留下任何基线痕迹，run.started 载荷与老路径一致
    audit_rows = registry_conn.execute(
        "SELECT action, detail_json FROM audit_log "
        "WHERE entity = ? ORDER BY id", (f"run:{legacy_id}",),
    ).fetchall()
    actions = [row[0] for row in audit_rows]
    assert "baseline.aligned" not in actions
    started = next(json.loads(row[1]) for row in audit_rows if row[0] == "run.started")
    assert "baseline" not in started


def test_disabled_failed_run_keeps_legacy_empty_summary(
    registry_conn, catalog_and_dir, fixture_tree, sql_roots, monkeypatch
):
    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    catalog, datasets_dir = catalog_and_dir
    monkeypatch.setattr("app.core.snapshot.run_metric", boom)
    with pytest.raises(Exception):
        _run(registry_conn, catalog, sql_roots, datasets_dir,
             keys=["active_users"], compare_baseline=False)
    row = registry_conn.execute(
        "SELECT summary_json FROM runs WHERE status = 'failed' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    # 老版本失败 run 同样写入空 summary 对象
    assert json.loads(row[0]) == {}


# ---------------------------------------------------------------------------
# 6. 报告渲染与 API 入口
# ---------------------------------------------------------------------------

def test_reports_include_baseline_section_when_present(
    registry_conn, catalog_and_dir, fixture_tree, sql_roots
):
    catalog, datasets_dir = catalog_and_dir
    _run(registry_conn, catalog, sql_roots, datasets_dir,
         keys=["active_users", "avg_order_value"])
    second_id = _run(registry_conn, catalog, sql_roots, datasets_dir,
                     keys=["active_users", "avg_order_value"])
    report = build_report(registry_conn, second_id)
    # JSON 报告天然透传扩展字段
    assert report["run"]["summary"]["baseline_comparison"]["status"] == "ok"

    _, markdown = export_report(report, "md")
    assert "## 批次基线对照" in markdown
    assert "active_users" in markdown
    assert "unchanged" in markdown

    _, html = export_report(report, "html")
    assert "批次基线对照" in html
    assert "<script" not in html


def test_api_supports_baseline_options(imported_client, app_config):
    payload = {"metrics": ["active_users"], "datasets_dir": str(app_config.datasets_dir)}
    first = imported_client.post("/api/runs", json=payload)
    baseline_id = first.get_json()["run"]["id"]

    # 默认开启对照
    second = imported_client.post("/api/runs", json=payload)
    comparison = second.get_json()["run"]["summary"]["baseline_comparison"]
    assert comparison["baseline_run_id"] == baseline_id

    # 显式关闭
    third = imported_client.post(
        "/api/runs", json={**payload, "compare_baseline": False}
    )
    assert "baseline_comparison" not in third.get_json()["run"]["summary"]

    # 非法参数被 400 拒绝
    bad = imported_client.post(
        "/api/runs", json={**payload, "baseline_run_id": "abc"}
    )
    assert bad.status_code == 400
    bad_bool = imported_client.post(
        "/api/runs", json={**payload, "compare_baseline": "no"}
    )
    assert bad_bool.status_code == 400
