from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from app.core.arbitration import (
    DEFAULT_CONSENSUS_THRESHOLD,
    DEFAULT_STRATEGY,
    arbitrate,
    validate_threshold,
)
from app.core.dataset_loader import DatasetCatalog
from app.core.registry import MetricRegistry
from app.core.reporter import build_report, export_report
from app.core.snapshot import get_run, perform_run, snapshots_for_run
from app.errors import CaliberError
from app.models import ImplFailure, MetricDefinition

THREE_IMPLS = {
    "impl_a": {"2026-01-01": 10.0, "2026-01-02": 20.0, "2026-01-03": 30.0},
    "impl_b": {"2026-01-01": 10.0, "2026-01-02": 20.0, "2026-01-03": 30.0},
    "impl_c": {"2026-01-01": 10.0, "2026-01-02": 120.0, "2026-01-03": 30.0},
}


def test_three_impls_two_close_one_deviates():
    result = arbitrate(THREE_IMPLS, threshold=1.0)
    assert result["status"] == "ok"
    assert result["consensus_group"] == ["impl_a", "impl_b"]
    assert [item["impl_id"] for item in result["dissenters"]] == ["impl_c"]
    assert result["dissent_dimensions"] == ["2026-01-02"]
    assert result["dissenters"][0]["dissent_dimensions"] == ["2026-01-02"]
    assert result["max_divergence"] == pytest.approx(100.0)
    assert result["max_divergence_detail"]["dimension"] == "2026-01-02"
    assert result["impl_classification"] == {
        "impl_a": "consensus",
        "impl_b": "consensus",
        "impl_c": "dissent",
    }
    assert result["summary"]["consensus_count"] == 2
    assert result["summary"]["dissenter_count"] == 1


def test_two_impls_disagree_degrades_without_forcing_consensus():
    result = arbitrate(
        {"impl_a": {"day": 10.0}, "impl_b": {"day": 12.0}}, threshold=1.0
    )
    assert result["status"] == "degraded"
    assert result["degraded"]["code"] == "two_impl_disagreement"
    assert result["consensus_group"] == []
    assert result["impl_classification"] == {
        "impl_a": "disputed",
        "impl_b": "disputed",
    }
    assert result["dissent_dimensions"] == ["day"]
    assert result["dissenters"]


def test_all_pairwise_disagreement_degrades():
    result = arbitrate(
        {
            "impl_a": {"day": 1.0},
            "impl_b": {"day": 2.0},
            "impl_c": {"day": 3.0},
        },
        threshold=1.0,
    )
    assert result["status"] == "degraded"
    assert result["degraded"]["code"] == "no_consensus"
    assert result["consensus_group"] == []
    assert {item["impl_id"] for item in result["dissenters"]} == {
        "impl_a",
        "impl_b",
        "impl_c",
    }
    assert result["max_divergence"] == pytest.approx(2.0)


def test_failed_impl_is_non_arbitrable_and_never_a_dissenter():
    failure = ImplFailure(
        impl_id="impl_c",
        source="arb_three",
        sql_file="arb_c.sql",
        stage="timeout",
        code="evaluation_timeout",
        reason="实现 impl_c 查询超时（0.05s）",
    )
    results = {key: value for key, value in THREE_IMPLS.items() if key != "impl_c"}
    result = arbitrate(results, [failure], threshold=1.0)
    assert result["status"] == "ok"
    assert result["consensus_group"] == ["impl_a", "impl_b"]
    assert [item["impl_id"] for item in result["dissenters"]] == []
    assert result["non_arbitrable"][0]["impl_id"] == "impl_c"
    assert "超时" in result["non_arbitrable"][0]["reason"]
    assert result["impl_classification"]["impl_c"] == "non_arbitrable"
    assert result["summary"]["non_arbitrable_count"] == 1


def test_threshold_allows_close_implementations_to_share_a_group():
    results = {
        "impl_a": {"d1": 1.0, "d2": 2.0, "d3": 3.0},
        "impl_b": {"d1": 1.0, "d2": 2.0, "d3": 3.0},
        "impl_c": {"d1": 1.0, "d2": 2.0, "d3": 99.0},
    }
    strict = arbitrate(results, threshold=1.0)
    assert strict["status"] == "ok"
    assert strict["consensus_group"] == ["impl_a", "impl_b"]
    assert strict["dissenters"][0]["impl_id"] == "impl_c"

    relaxed = arbitrate(results, threshold=0.6)
    assert relaxed["status"] == "ok"
    assert relaxed["consensus_group"] == ["impl_a", "impl_b", "impl_c"]
    assert relaxed["dissenters"] == []


def test_minority_inside_consensus_group_is_not_swallowed():
    results = {
        "impl_a": {"d1": 1.0, "d2": 2.0, "d3": 3.0},
        "impl_b": {"d1": 1.0, "d2": 2.0, "d3": 3.0},
        "impl_c": {"d1": 1.0, "d2": 2.0, "d3": 30.0},
        "impl_d": {"d1": 1.0, "d2": 2.0, "d3": 30.0},
    }
    result = arbitrate(results, threshold=0.5)
    assert result["status"] == "ok"
    assert result["consensus_group"] == ["impl_a", "impl_b", "impl_c", "impl_d"]
    assert result["minority"]
    minority_dimensions = {
        entry["impl_id"]: entry["minority_dimensions"] for entry in result["minority"]
    }
    assert minority_dimensions["impl_a"] == ["d3"]
    assert minority_dimensions["impl_d"] == ["d3"]


def test_dimension_vote_strategy_is_switchable():
    result = arbitrate(THREE_IMPLS, threshold=1.0, strategy="dimension_vote")
    assert result["strategy"] == "dimension_vote"
    assert result["status"] == "ok"
    assert result["consensus_group"] == ["impl_a", "impl_b"]
    assert result["dissenters"][0]["dissent_dimensions"] == ["2026-01-02"]
    assert result["selection"]["chosen"] == DEFAULT_STRATEGY
    assert result["selection"]["active"] == "dimension_vote"
    assert "switch_point" in result["selection"]
    assert result["selection"]["rationale"]


def test_arbitration_is_deterministic_for_same_input():
    first = arbitrate(THREE_IMPLS, threshold=1.0)
    second = arbitrate(copy.deepcopy(THREE_IMPLS), threshold=1.0)
    assert first == second


def test_internal_error_degrades_and_keeps_payload_shape(monkeypatch):
    import app.core.arbitration as module

    def broken(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(module, "_arbitrate_impl_cluster", broken)
    result = arbitrate(THREE_IMPLS, threshold=1.0)
    assert result["status"] == "degraded"
    assert result["degraded"]["code"] == "arbitration_error"
    assert "boom" in result["degraded"]["detail"]
    assert result["consensus_group"] == []
    assert result["pairwise"] == []


def test_threshold_validation():
    assert validate_threshold(None) is None
    assert validate_threshold(0.3) == 0.3
    with pytest.raises(CaliberError):
        validate_threshold(-0.1)
    with pytest.raises(CaliberError):
        validate_threshold(1.5)
    with pytest.raises(CaliberError):
        validate_threshold("abc")


def test_default_threshold_is_documented_value():
    assert DEFAULT_CONSENSUS_THRESHOLD == 1.0
    assert DEFAULT_STRATEGY == "impl_cluster"


# ---------------------------------------------------------------------------
# integration: snapshots, run-level resilience and report export
# ---------------------------------------------------------------------------


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _three_impl_metric() -> MetricDefinition:
    return MetricDefinition(
        key="arb_three_impls",
        name="三实现仲裁",
        owner="qa",
        description="两个实现接近，第三个实现单维偏离",
        unit="个",
        grain="day",
        impls=[
            _impl("impl_a", "arb_three", "arb_base.sql"),
            _impl("impl_b", "arb_three", "arb_base.sql"),
            _impl("impl_c", "arb_three", "arb_deviate.sql"),
        ],
    )


def _two_impl_metric() -> MetricDefinition:
    return MetricDefinition(
        key="arb_two_impls",
        name="两实现互斥",
        owner="qa",
        description="两个实现互不相让",
        unit="个",
        grain="day",
        impls=[
            _impl("impl_a", "arb_three", "arb_base.sql"),
            _impl("impl_b", "arb_three", "arb_offset.sql"),
        ],
    )


def _timeout_metric() -> MetricDefinition:
    return MetricDefinition(
        key="arb_timeout_impl",
        name="超时实现",
        owner="qa",
        description="其中一个实现超时",
        unit="个",
        grain="day",
        impls=[
            _impl("impl_a", "arb_three", "arb_base.sql"),
            _impl("impl_b", "arb_three", "arb_slow.sql"),
            _impl("impl_c", "arb_three", "arb_base.sql"),
        ],
    )


def _impl(impl_id: str, source: str, sql_file: str):
    from app.models import ImplementDef

    return ImplementDef(id=impl_id, source=source, sql_file=sql_file)


def _prepare_sql(fixture_tree: Path) -> None:
    _write(
        fixture_tree / "impls" / "arb_base.sql",
        "SELECT day, val FROM arb_three ORDER BY day",
    )
    _write(
        fixture_tree / "impls" / "arb_deviate.sql",
        "SELECT day, CASE WHEN day = '2026-01-02' THEN val + 100 ELSE val END AS val "
        "FROM arb_three ORDER BY day",
    )
    _write(
        fixture_tree / "impls" / "arb_offset.sql",
        "SELECT day, val + 100 AS val FROM arb_three ORDER BY day",
    )
    _write(
        fixture_tree / "impls" / "arb_slow.sql",
        "WITH RECURSIVE cnt(x) AS (VALUES(1) UNION ALL "
        "SELECT x + 1 FROM cnt WHERE x < 100000000) "
        "SELECT '2026-01-01', COUNT(*) FROM cnt",
    )


def _prepare_dataset(fixture_tree: Path) -> None:
    _write(
        fixture_tree / "datasets" / "arb_three.csv",
        "day,val\n2026-01-01,10\n2026-01-02,20\n",
    )


def _import_metrics(conn, fixture_tree: Path, sql_roots) -> None:
    registry = MetricRegistry(conn, sql_roots)
    for metric in (_three_impl_metric(), _two_impl_metric(), _timeout_metric()):
        registry.import_definition(metric, source_file="<test>")


def _run(conn, fixture_tree: Path, sql_roots, metrics=None, threshold=1.0):
    catalog = DatasetCatalog(fixture_tree / "datasets")
    try:
        return perform_run(
            conn,
            metrics,
            catalog,
            sql_roots,
            datasets_dir=fixture_tree / "datasets",
            timeout_seconds=0.05,
            max_rows=100,
            abs_tolerance=1e-6,
            rel_tolerance=0.0,
            consensus_threshold=threshold,
        )
    finally:
        catalog.close()


def test_perform_run_arbitration_is_written_to_snapshot(
    conn, fixture_tree: Path, sql_roots
):
    _prepare_sql(fixture_tree)
    _prepare_dataset(fixture_tree)
    _import_metrics(conn, fixture_tree, sql_roots)

    run_id = _run(conn, fixture_tree, sql_roots, ["arb_three_impls"])
    snapshot = snapshots_for_run(conn, run_id)[0]
    arbitration = snapshot["result"]["arbitration"]
    assert arbitration["status"] == "ok"
    assert arbitration["consensus_group"] == ["impl_a", "impl_b"]
    assert arbitration["dissenters"][0]["impl_id"] == "impl_c"
    assert arbitration["dissent_dimensions"] == ["2026-01-02"]
    assert set(snapshot["impl_ids"]) == {"impl_a", "impl_b", "impl_c"}

    report = build_report(conn, run_id)
    metric = report["metrics"][0]
    assert metric["arbitration"]["consensus_group"] == ["impl_a", "impl_b"]
    assert report["summary"]["arbitration"]["enabled"] is True

    _, raw_json = export_report(report, "json")
    assert json.loads(raw_json)["metrics"][0]["arbitration"]["dissenters"][0][
        "impl_id"
    ] == "impl_c"
    _, markdown = export_report(report, "md")
    assert "跨实现仲裁" in markdown
    assert "impl_c" in markdown
    _, rendered_html = export_report(report, "html")
    assert "跨实现仲裁" in rendered_html
    assert "impl_c" in rendered_html


def test_mixed_run_survives_disagreement_and_timeout(
    conn, fixture_tree: Path, sql_roots
):
    _prepare_sql(fixture_tree)
    _prepare_dataset(fixture_tree)
    _import_metrics(conn, fixture_tree, sql_roots)

    run_id = _run(
        conn,
        fixture_tree,
        sql_roots,
        ["arb_three_impls", "arb_two_impls", "arb_timeout_impl"],
    )
    run = get_run(conn, run_id)
    assert run["status"] == "completed"
    snapshots = {item["metric_key"]: item for item in snapshots_for_run(conn, run_id)}
    assert set(snapshots) == {"arb_three_impls", "arb_two_impls", "arb_timeout_impl"}

    two = snapshots["arb_two_impls"]["result"]["arbitration"]
    assert two["status"] == "degraded"
    assert two["degraded"]["code"] == "two_impl_disagreement"

    timeout = snapshots["arb_timeout_impl"]
    arbitration = timeout["result"]["arbitration"]
    assert len(arbitration["non_arbitrable"]) == 1
    assert arbitration["non_arbitrable"][0]["impl_id"] == "impl_b"
    assert arbitration["non_arbitrable"][0]["stage"] == "timeout"
    assert arbitration["non_arbitrable"][0]["reason"]
    assert "impl_b" not in [item["impl_id"] for item in arbitration["dissenters"]]
    assert arbitration["consensus_group"] == ["impl_a", "impl_c"]
    # Existing dimension comparison results are preserved.
    assert timeout["result"]["diffs"] == []
    assert timeout["result"]["dimensions"] == ["2026-01-01", "2026-01-02"]

    report = build_report(conn, run_id)
    _, markdown = export_report(report, "md")
    assert "不可仲裁实现" in markdown
    _, rendered_html = export_report(report, "html")
    assert "不可仲裁实现" in rendered_html


def test_arbitration_result_is_stable_across_runs(
    conn, fixture_tree: Path, sql_roots
):
    _prepare_sql(fixture_tree)
    _prepare_dataset(fixture_tree)
    _import_metrics(conn, fixture_tree, sql_roots)

    first = _run(conn, fixture_tree, sql_roots, ["arb_three_impls"])
    second = _run(conn, fixture_tree, sql_roots, ["arb_three_impls"])
    first_payload = snapshots_for_run(conn, first)[0]["result"]["arbitration"]
    second_payload = snapshots_for_run(conn, second)[0]["result"]["arbitration"]
    assert first_payload == second_payload


def test_run_without_threshold_keeps_legacy_snapshot_and_report(
    registry_conn, execute_fixture_run
):
    run_id = execute_fixture_run()
    snapshots = snapshots_for_run(registry_conn, run_id)
    for snapshot in snapshots:
        assert "arbitration" not in snapshot["result"]
        assert "impl_failures" not in snapshot["result"]
        assert "arbitration_status" not in snapshot["summary"]

    report = build_report(registry_conn, run_id)
    assert "arbitration" not in report["summary"]
    for metric in report["metrics"]:
        assert "arbitration" not in metric

    for fmt in ("json", "md", "html"):
        fmt_name, content = export_report(report, fmt)
        assert fmt_name == fmt
        assert content


def test_arbitration_api_switch(imported_client, app_config):
    response = imported_client.post(
        "/api/runs",
        json={
            "metrics": ["avg_order_value"],
            "datasets_dir": str(app_config.datasets_dir),
            "arbitration": {"threshold": 1.0, "strategy": "impl_cluster"},
        },
    )
    assert response.status_code == 201
    snapshot = response.get_json()["snapshots"][0]
    assert snapshot["result"]["arbitration"]["strategy"] == "impl_cluster"

    run_id = response.get_json()["run"]["id"]
    page = imported_client.get(f"/reports/{run_id}")
    assert page.status_code == 200
    body = page.get_data(as_text=True)
    assert "跨实现仲裁" in body
    assert "degraded" in body
    assert "两个实现互不一致" in body

    response = imported_client.post(
        "/api/runs",
        json={
            "metrics": ["avg_order_value"],
            "datasets_dir": str(app_config.datasets_dir),
        },
    )
    assert response.status_code == 201
    assert "arbitration" not in response.get_json()["snapshots"][0]["result"]

    response = imported_client.post(
        "/api/runs",
        json={
            "metrics": ["avg_order_value"],
            "datasets_dir": str(app_config.datasets_dir),
            "arbitration": {"threshold": 2.0},
        },
    )
    assert response.status_code == 400


def test_old_snapshot_without_arbitration_still_exports(
    conn, fixture_tree: Path, sql_roots
):
    """A snapshot written before this feature must still export cleanly."""
    _prepare_sql(fixture_tree)
    _prepare_dataset(fixture_tree)
    _import_metrics(conn, fixture_tree, sql_roots)
    run_id = _run(conn, fixture_tree, sql_roots, ["arb_three_impls"], threshold=None)

    row = conn.execute(
        "SELECT result_json, summary_json FROM snapshots WHERE run_id = ?", (run_id,)
    ).fetchone()
    result = json.loads(row["result_json"])
    assert "arbitration" not in result
    assert "impl_failures" not in result

    report = build_report(conn, run_id)
    metric = report["metrics"][0]
    assert "arbitration" not in metric
    assert "arbitration" not in report["summary"]
    fmt, markdown = export_report(report, "md")
    assert fmt == "md" and "跨实现仲裁" not in markdown
    _, rendered = export_report(report, "html")
    assert "跨实现仲裁" not in rendered
    decoded = json.loads(export_report(report, "json")[1])
    assert "arbitration" not in decoded["metrics"][0]
    assert decoded["report_version"] == "1.0"
