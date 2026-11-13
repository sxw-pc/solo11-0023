"""维度裁剪：排序落库、三种视图、旧快照降级与 result_json 不可变性的验收测试。"""
from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from app.core.ranking import DEGRADED_NOTICE, normalize_view
from app.core.reporter import build_report, export_report
from app.core.snapshot import (
    create_run,
    finish_run,
    get_run,
    get_snapshot,
    save_snapshot,
    snapshots_for_run,
)
from app.errors import ReportError
from app.models import (
    ComparisonResult,
    DimensionDiff,
    ImplementDef,
    ImplEvaluation,
    MetricDefinition,
    utc_now,
)
from cli.main import main


def _metric() -> MetricDefinition:
    return MetricDefinition(
        key="synthetic",
        name="合成指标",
        owner="test",
        description="",
        unit="个",
        grain="day",
        impls=[
            ImplementDef(id="impl_a", source="ds_a", sql_file="a.sql"),
            ImplementDef(id="impl_b", source="ds_b", sql_file="b.sql"),
        ],
    )


def _save_synthetic(conn, pairs: list[tuple[str, str, float | None]]) -> tuple[int, int]:
    """按 (dimension, kind, max_delta) 列表直接落一条合成快照。"""
    run_id = create_run(conn, ["synthetic"], "ds", {"abs": 0.0, "rel": 0.0})
    diffs = [
        DimensionDiff(
            dimension=name,
            kind=kind,
            values={"impl_a": 1.0, "impl_b": None},
            max_delta=delta,
            detail="",
        )
        for name, kind, delta in pairs
    ]
    comparison = ComparisonResult(
        metric_key="synthetic",
        metric_version=1,
        impl_ids=["impl_a", "impl_b"],
        verdict="inconsistent",
        dimensions=[name for name, _, _ in pairs],
        diffs=diffs,
        summary={"diff_count": len(diffs), "dimension_count": len(diffs)},
        abs_tolerance=0.0,
        rel_tolerance=0.0,
    )
    evaluations = {
        impl_id: ImplEvaluation(
            impl_id=impl_id,
            source=f"ds_{impl_id[-1]}",
            sql_file=f"{impl_id}.sql",
            values={},
            row_count=0,
            duration_ms=0.1,
        )
        for impl_id in ("impl_a", "impl_b")
    }
    snapshot_id = save_snapshot(conn, run_id, _metric(), comparison, evaluations)
    finish_run(conn, run_id, status="completed", summary={})
    return run_id, snapshot_id


def _insert_legacy_snapshot(conn, *, summary: dict, result: dict) -> tuple[int, int]:
    """模拟老版本落库的快照：summary 无 dimension_ranking。"""
    run_id = create_run(conn, ["legacy_metric"], "ds", {"abs": 1e-6, "rel": 0.0})
    finish_run(conn, run_id, status="completed", summary={})
    cursor = conn.execute(
        "INSERT INTO snapshots(run_id, metric_key, metric_version, datasets_json,"
        " impl_ids_json, verdict, summary_json, result_json, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            run_id,
            "legacy_metric",
            1,
            json.dumps(["ds_a"], ensure_ascii=False),
            json.dumps(["impl_a", "impl_b"], ensure_ascii=False),
            "inconsistent",
            json.dumps(summary, ensure_ascii=False),
            json.dumps(result, ensure_ascii=False),
            utc_now(),
        ),
    )
    conn.commit()
    return run_id, int(cursor.lastrowid)


def _legacy_result() -> dict:
    return {
        "metric_key": "legacy_metric",
        "metric_version": 1,
        "impl_ids": ["impl_a", "impl_b"],
        "verdict": "inconsistent",
        "dimensions": ["2026-01", "2026-02"],
        "diffs": [
            {
                "dimension": "2026-01",
                "kind": "value_mismatch",
                "values": {"impl_a": 1.0, "impl_b": 4.0},
                "max_delta": 3.0,
                "detail": "数值超出容差",
            },
            {
                "dimension": "2026-02",
                "kind": "value_mismatch",
                "values": {"impl_a": 2.0, "impl_b": 9.0},
                "max_delta": 7.0,
                "detail": "数值超出容差",
            },
        ],
        "summary": {"diff_count": 2, "dimension_count": 2},
        "abs_tolerance": 1e-6,
        "rel_tolerance": 0.0,
        "metric": {
            "key": "legacy_metric",
            "name": "旧指标",
            "owner": "test",
            "description": "",
            "unit": "个",
            "grain": "day",
            "impls": [],
        },
        "implementations": [],
    }


def _raw_column(conn, snapshot_id: int, column: str) -> str:
    assert column in {"result_json", "summary_json"}
    row = conn.execute(
        f"SELECT {column} FROM snapshots WHERE id = ?", (snapshot_id,)
    ).fetchone()
    return row[0]


def test_ranking_persisted_at_run_time_with_strategy_metadata(
    registry_conn, execute_fixture_run
):
    run_id = execute_fixture_run()
    snapshots = snapshots_for_run(registry_conn, run_id)
    assert snapshots
    for snapshot in snapshots:
        ranking = snapshot["summary"]["dimension_ranking"]
        assert ranking["generated_by"] == "run"
        assert ranking["degraded"] is False
        entries = ranking["entries"]
        assert [entry["rank"] for entry in entries] == list(range(1, len(entries) + 1))
    strategy = get_run(registry_conn, run_id)["summary"]["dimension_ranking"]
    assert strategy["strategy"] == "persisted_at_run"
    assert "result_json" in strategy["reason"]


def test_ranking_order_matches_manual_sort_with_none_and_extremes(conn):
    pairs = [
        ("dim_finite_small", "value_mismatch", 0.5),
        ("dim_finite_big", "value_mismatch", 12.25),
        ("dim_finite_mid", "value_mismatch", 3.0),
        ("dim_extreme", "value_mismatch", 1e300),
        ("dim_inf", "value_mismatch", float("inf")),
        ("dim_ninf", "value_mismatch", float("-inf")),
        ("dim_nan", "value_mismatch", float("nan")),
        ("dim_missing_b", "missing_dimension", None),
        ("dim_missing_a", "missing_dimension", None),
    ]
    _, snapshot_id = _save_synthetic(conn, pairs)
    ranking = get_snapshot(conn, snapshot_id)["summary"]["dimension_ranking"]
    # 人工排序：有限值按 |差值| 降序 → 非有限按维度名 → 缺失按维度名
    assert [entry["dimension"] for entry in ranking["entries"]] == [
        "dim_extreme",
        "dim_finite_big",
        "dim_finite_mid",
        "dim_finite_small",
        "dim_inf",
        "dim_nan",
        "dim_ninf",
        "dim_missing_a",
        "dim_missing_b",
    ]
    by_name = {entry["dimension"]: entry for entry in ranking["entries"]}
    assert by_name["dim_extreme"]["weight"] == pytest.approx(1.0)
    assert by_name["dim_finite_big"]["max_delta"] == 12.25
    for name in ("dim_inf", "dim_ninf", "dim_nan", "dim_missing_a", "dim_missing_b"):
        assert by_name[name]["weight"] is None
    finite_total = 1e300 + 12.25 + 3.0 + 0.5
    assert ranking["total_delta"] == finite_total
    finite_weights = [
        entry["weight"] for entry in ranking["entries"] if entry["weight"] is not None
    ]
    assert sum(finite_weights) == pytest.approx(1.0)


def test_ranking_weights_share_of_finite_total(conn):
    pairs = [
        ("d5", "value_mismatch", 5.0),
        ("d3", "value_mismatch", 3.0),
        ("d2", "value_mismatch", 2.0),
        ("dm", "missing_dimension", None),
    ]
    _, snapshot_id = _save_synthetic(conn, pairs)
    ranking = get_snapshot(conn, snapshot_id)["summary"]["dimension_ranking"]
    weights = {entry["dimension"]: entry["weight"] for entry in ranking["entries"]}
    assert weights["d5"] == 0.5
    assert weights["d3"] == pytest.approx(0.3)
    assert weights["d2"] == pytest.approx(0.2)
    assert weights["dm"] is None


def test_same_input_twice_produces_identical_summary(conn):
    pairs = [
        ("b", "value_mismatch", 2.5),
        ("a", "value_mismatch", 2.5),
        ("c", "missing_dimension", None),
        ("d", "value_mismatch", float("inf")),
    ]
    _, first_id = _save_synthetic(conn, pairs)
    _, second_id = _save_synthetic(conn, pairs)
    assert _raw_column(conn, first_id, "summary_json") == _raw_column(
        conn, second_id, "summary_json"
    )


def test_two_fixture_runs_produce_identical_rankings(registry_conn, execute_fixture_run):
    first_run = execute_fixture_run()
    second_run = execute_fixture_run()
    first = {
        item["metric_key"]: item["summary"]["dimension_ranking"]
        for item in snapshots_for_run(registry_conn, first_run)
    }
    second = {
        item["metric_key"]: item["summary"]["dimension_ranking"]
        for item in snapshots_for_run(registry_conn, second_run)
    }
    assert first == second


def test_top_view_orders_by_ranking_and_limits(conn):
    pairs = [
        ("zeta", "value_mismatch", 1.0),
        ("alpha", "value_mismatch", 9.0),
        ("mid", "value_mismatch", 4.0),
    ]
    run_id, _ = _save_synthetic(conn, pairs)
    report = build_report(conn, run_id, view="top", top=2)
    assert report["view"] == {
        "mode": "top",
        "top": 2,
        "dimension": None,
        "label": "前 2 个（按 |差值| 降序）",
    }
    item = report["metrics"][0]
    assert [diff["dimension"] for diff in item["diffs"]] == ["alpha", "mid"]
    assert item["diffs_shown"] == 2
    assert item["diffs_total"] == 3


def test_dimension_view_selects_single_dimension(conn):
    pairs = [
        ("keep", "value_mismatch", 1.0),
        ("drop", "value_mismatch", 8.0),
    ]
    run_id, _ = _save_synthetic(conn, pairs)
    report = build_report(conn, run_id, view="dimension", dimension="keep")
    item = report["metrics"][0]
    assert [diff["dimension"] for diff in item["diffs"]] == ["keep"]
    assert item["diffs_shown"] == 1
    assert item["diffs_total"] == 2

    missing = build_report(conn, run_id, view="dimension", dimension="nothere")
    assert missing["metrics"][0]["diffs"] == []
    assert missing["metrics"][0]["diffs_total"] == 2


def test_all_view_is_default_and_keeps_original_order(conn):
    pairs = [
        ("b_dim", "value_mismatch", 1.0),
        ("a_dim", "value_mismatch", 9.0),
    ]
    run_id, _ = _save_synthetic(conn, pairs)
    report = build_report(conn, run_id)
    item = report["metrics"][0]
    assert report["view"]["mode"] == "all"
    # all 视图保持 result_json 中的原始存储顺序，不按差值重排
    assert [diff["dimension"] for diff in item["diffs"]] == ["b_dim", "a_dim"]
    assert item["diffs_shown"] == item["diffs_total"] == 2


def test_ranking_tolerates_malformed_diff_entries():
    from app.core.ranking import rank_diffs

    ranking = rank_diffs(
        [
            "not-a-dict",
            {"kind": "value_mismatch", "max_delta": 1.0},  # 缺 dimension
            {"dimension": "dup", "kind": "value_mismatch", "max_delta": 2.0},
            {"dimension": "dup", "kind": "value_mismatch", "max_delta": 99.0},  # 重名
            {"dimension": "bool_delta", "kind": "x", "max_delta": True},  # 非常规类型
            {"dimension": "ok", "kind": "value_mismatch", "max_delta": 5},
        ],
        generated_by="backfill",
    )
    names = [entry["dimension"] for entry in ranking["entries"]]
    assert names == ["ok", "dup", "bool_delta"]  # 有限 5.0 → 2.0 → 缺失档
    by_name = {entry["dimension"]: entry for entry in ranking["entries"]}
    assert by_name["dup"]["max_delta"] == 2.0  # 重名保留先出现者
    assert by_name["bool_delta"]["max_delta"] is None  # 布尔按缺失处理
    assert by_name["ok"]["max_delta"] == 5.0


def test_invalid_view_params_raise():
    with pytest.raises(ReportError):
        normalize_view("bogus")
    with pytest.raises(ReportError):
        normalize_view("top", top=0)
    with pytest.raises(ReportError):
        normalize_view("top", top="abc")
    with pytest.raises(ReportError):
        normalize_view("dimension")
    assert normalize_view("top")["top"] == 10


def test_legacy_snapshot_backfilled_once_and_result_json_untouched(conn):
    run_id, snapshot_id = _insert_legacy_snapshot(
        conn,
        summary={"diff_count": 2, "dimension_count": 2},
        result=_legacy_result(),
    )
    before_result = _raw_column(conn, snapshot_id, "result_json")
    assert "dimension_ranking" not in _raw_column(conn, snapshot_id, "summary_json")

    report = build_report(conn, run_id, view="top", top=1)
    item = report["metrics"][0]
    ranking = item["summary"]["dimension_ranking"]
    assert ranking["generated_by"] == "backfill"
    assert ranking["degraded"] is False
    assert [entry["dimension"] for entry in ranking["entries"]] == ["2026-02", "2026-01"]
    assert [diff["dimension"] for diff in item["diffs"]] == ["2026-02"]

    # 补算结果已回写摘要，且 result_json 一个字节都没动
    persisted = json.loads(_raw_column(conn, snapshot_id, "summary_json"))
    assert persisted["dimension_ranking"]["generated_by"] == "backfill"
    assert _raw_column(conn, snapshot_id, "result_json") == before_result

    again = build_report(conn, run_id, view="top", top=1)
    assert [diff["dimension"] for diff in again["metrics"][0]["diffs"]] == ["2026-02"]


def test_legacy_snapshot_without_diff_details_degrades_with_notice(conn):
    result = _legacy_result()
    del result["diffs"]
    result["dimensions"] = ["zeta", "alpha", "mid"]
    result["verdict"] = "partial"
    run_id, snapshot_id = _insert_legacy_snapshot(
        conn, summary={"dimension_count": 3}, result=result
    )
    before_result = _raw_column(conn, snapshot_id, "result_json")

    report = build_report(conn, run_id)
    item = report["metrics"][0]
    ranking = item["summary"]["dimension_ranking"]
    assert ranking["degraded"] is True
    assert item["ranking_degraded"] is True
    assert item["ranking_notice"] == DEGRADED_NOTICE
    assert [entry["dimension"] for entry in ranking["entries"]] == ["alpha", "mid", "zeta"]
    assert all(entry["weight"] is None for entry in ranking["entries"])

    # 报告链不中断：三种格式都能导出，且 Markdown 带显式降级提示
    for fmt in ("json", "md", "html"):
        _, payload = export_report(report, fmt)
        assert payload
    _, markdown = export_report(report, "md")
    assert "降级" in markdown
    _, rendered_html = export_report(report, "html")
    assert "注意" in rendered_html
    assert _raw_column(conn, snapshot_id, "result_json") == before_result


def test_repeated_exports_are_stable(conn):
    pairs = [
        ("d1", "value_mismatch", 5.0),
        ("d2", "value_mismatch", 3.0),
        ("d3", "missing_dimension", None),
        ("d4", "value_mismatch", float("inf")),
    ]
    run_id, _ = _save_synthetic(conn, pairs)
    md_exports = []
    json_views = []
    for _ in range(2):
        report = build_report(conn, run_id, view="top", top=3)
        _, markdown = export_report(report, "md")
        md_exports.append(markdown)
        _, raw_json = export_report(report, "json")
        decoded = json.loads(raw_json)
        decoded.pop("generated_at")
        json_views.append(decoded)
    assert md_exports[0] == md_exports[1]
    assert json_views[0] == json_views[1]
    assert [
        diff["dimension"] for diff in json_views[0]["metrics"][0]["diffs"]
    ] == ["d1", "d2", "d4"]


def test_result_json_immutable_across_all_views(conn):
    pairs = [
        ("x", "value_mismatch", 2.0),
        ("y", "value_mismatch", 6.0),
        ("z", "missing_dimension", None),
    ]
    run_id, snapshot_id = _save_synthetic(conn, pairs)
    before = _raw_column(conn, snapshot_id, "result_json")
    for view_kwargs in (
        {"view": "all"},
        {"view": "top", "top": 2},
        {"view": "dimension", "dimension": "y"},
    ):
        report = build_report(conn, run_id, **view_kwargs)
        for fmt in ("json", "md", "html"):
            export_report(report, fmt)
    assert _raw_column(conn, snapshot_id, "result_json") == before


def test_report_api_supports_three_views(imported_client, app_config):
    created = imported_client.post(
        "/api/runs",
        json={"metrics": ["avg_order_value"], "datasets_dir": str(app_config.datasets_dir)},
    )
    assert created.status_code == 201
    run_id = created.get_json()["run"]["id"]

    full = imported_client.get(f"/api/reports/{run_id}?format=json").get_json()
    metric = full["metrics"][0]
    assert full["view"]["mode"] == "all"
    total = metric["diffs_total"]
    assert total >= 2
    target = metric["diffs"][0]["dimension"]

    top = imported_client.get(f"/api/reports/{run_id}?format=json&view=top&top=1")
    assert top.status_code == 200
    top_metric = top.get_json()["metrics"][0]
    assert top.get_json()["view"] == {
        "mode": "top",
        "top": 1,
        "dimension": None,
        "label": "前 1 个（按 |差值| 降序）",
    }
    assert len(top_metric["diffs"]) == 1
    assert top_metric["diffs_total"] == total
    # 前 1 名必须是排序结果里 |差值| 最大的维度
    ranking = top_metric["summary"]["dimension_ranking"]["entries"]
    assert top_metric["diffs"][0]["dimension"] == ranking[0]["dimension"]

    single = imported_client.get(
        f"/api/reports/{run_id}?format=json&view=dimension&dimension={target}"
    )
    assert single.status_code == 200
    single_metric = single.get_json()["metrics"][0]
    assert [diff["dimension"] for diff in single_metric["diffs"]] == [target]

    markdown = imported_client.get(f"/api/reports/{run_id}?format=md&view=top&top=1")
    assert markdown.status_code == 200
    assert "前 1 个" in markdown.get_data(as_text=True)

    assert imported_client.get(f"/api/reports/{run_id}?view=bogus").status_code == 400
    assert (
        imported_client.get(f"/api/reports/{run_id}?view=top&top=abc").status_code == 400
    )
    assert imported_client.get(f"/api/reports/{run_id}?view=dimension").status_code == 400


def test_report_page_accepts_view_params(imported_client, app_config):
    created = imported_client.post(
        "/api/runs",
        json={"metrics": ["avg_order_value"], "datasets_dir": str(app_config.datasets_dir)},
    )
    run_id = created.get_json()["run"]["id"]
    page = imported_client.get(f"/reports/{run_id}?view=top&top=1")
    assert page.status_code == 200
    body = page.get_data(as_text=True)
    assert "前 1 个" in body
    assert "显示 1 / 共" in body
    assert imported_client.get(f"/reports/{run_id}?view=bogus").status_code == 400


def test_cli_report_view_flags(tmp_path: Path, fixture_tree: Path, capsys):
    db_path = tmp_path / "cli-view.db"
    prefix = ["--db", str(db_path), "--fixtures-dir", str(fixture_tree)]
    assert main(prefix + ["import-metrics", "--dir", str(fixture_tree / "metrics")]) == 0
    assert main(prefix + ["run", "--datasets", str(fixture_tree / "datasets")]) == 0
    capsys.readouterr()

    assert main(prefix + ["report", "--last", "--format", "md", "--view", "top", "--top", "1"]) == 0
    top_output = capsys.readouterr().out
    assert "前 1 个" in top_output
    assert "显示 1 / 共" in top_output

    assert main(prefix + ["report", "--last", "--format", "json", "--view", "all"]) == 0
    decoded = json.loads(capsys.readouterr().out)
    assert decoded["view"]["mode"] == "all"

    code = main(prefix + ["report", "--last", "--view", "dimension"])
    assert code == 1
    assert "dimension" in capsys.readouterr().err
