from __future__ import annotations

import json
from pathlib import Path


def test_metric_query_api(imported_client):
    response = imported_client.get("/api/metrics")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["count"] == 3

    response = imported_client.get("/api/metrics/active_users")
    assert response.status_code == 200
    assert response.get_json()["metric"]["version"] == 1

    response = imported_client.get("/api/metrics/active_users/versions")
    assert response.status_code == 200
    assert response.get_json()["count"] == 1

    response = imported_client.get("/api/metrics/active_users/versions/1")
    assert response.status_code == 200
    assert len(response.get_json()["metric"]["definition"]["impls"]) == 2

    response = imported_client.get(
        "/api/metrics/active_users/diff?from=1&to=1"
    )
    assert response.status_code == 200
    assert response.get_json()["diff"]["changed"] is False

    assert imported_client.get("/api/metrics/unknown").status_code == 404
    assert imported_client.get("/api/metrics/active_users/versions/99").status_code == 404


def test_run_trigger_and_report_api(imported_client, app_config):
    response = imported_client.post(
        "/api/runs",
        json={
            "metrics": ["active_users", "conversion_rate", "avg_order_value"],
            "datasets_dir": str(app_config.datasets_dir),
            "abs": 1e-6,
            "rel": 0,
        },
    )
    assert response.status_code == 201
    payload = response.get_json()
    run_id = payload["run"]["id"]
    assert payload["run"]["status"] == "completed"
    verdicts = {item["metric_key"]: item["verdict"] for item in payload["snapshots"]}
    assert verdicts["active_users"] == "consistent"
    assert verdicts["avg_order_value"] == "inconsistent"

    runs = imported_client.get("/api/runs")
    assert runs.status_code == 200
    assert runs.get_json()["count"] == 1

    detail = imported_client.get(f"/api/runs/{run_id}")
    assert detail.status_code == 200
    assert len(detail.get_json()["snapshots"]) == 3
    assert imported_client.get("/api/runs/999").status_code == 404

    response = imported_client.get(f"/api/reports/{run_id}?format=json")
    assert response.status_code == 200
    report = json.loads(response.get_data(as_text=True))
    assert report["summary"]["metric_count"] == 3

    response = imported_client.get(f"/api/reports/{run_id}?format=md")
    assert response.status_code == 200
    assert response.mimetype == "text/markdown"
    assert "指标准星一致性报告" in response.get_data(as_text=True)

    response = imported_client.get(f"/api/reports/{run_id}?format=html")
    assert response.status_code == 200
    assert response.mimetype == "text/html"
    assert "http://" not in response.get_data(as_text=True)

    assert imported_client.get(f"/api/reports/{run_id}?format=pdf").status_code == 400
    assert imported_client.get("/api/reports/999").status_code == 404


def test_run_validation_errors(imported_client, app_config, tmp_path: Path):
    response = imported_client.post("/api/runs", json={"metrics": 123})
    assert response.status_code == 400
    assert response.get_json()["error"] == "caliber_error"

    response = imported_client.post(
        "/api/runs", json={"metrics": ["unknown_metric"]}
    )
    assert response.status_code == 404

    response = imported_client.post(
        "/api/runs",
        json={"metrics": ["active_users"], "datasets_dir": str(tmp_path / "missing")},
    )
    assert response.status_code == 400


def test_run_with_tolerance_fields(imported_client, app_config):
    response = imported_client.post(
        "/api/runs",
        json={
            "metrics": "avg_order_value",
            "datasets_dir": str(app_config.datasets_dir),
            "tolerances": {"abs": 1000, "rel": 1.0},
        },
    )
    assert response.status_code == 201
    assert response.get_json()["snapshots"][0]["verdict"] == "consistent"

    response = imported_client.post(
        "/api/runs",
        json={"metrics": ["active_users"], "abs": -1},
    )
    assert response.status_code == 400


def test_run_with_broken_dataset_returns_warnings_and_keeps_going(
    imported_client, app_config
):
    from app.core.dataset_loader import DatasetCatalog

    broken = app_config.datasets_dir / "broken_batch.csv"
    broken.write_text("a,b,c\n1,2\n", encoding="utf-8")
    try:
        catalog = DatasetCatalog(app_config.datasets_dir)
        assert any(item["dataset"] == "broken_batch" for item in catalog.load_errors)
        catalog.close()
        response = imported_client.post(
            "/api/runs",
            json={"metrics": ["active_users"], "datasets_dir": str(app_config.datasets_dir)},
        )
        assert response.status_code == 201
        warnings = response.get_json()["dataset_warnings"]
        assert any(item["dataset"] == "broken_batch" for item in warnings)
    finally:
        broken.unlink(missing_ok=True)


def test_report_api_emits_strict_json_with_non_finite_values(tmp_path: Path):
    import math

    from app import create_app
    from app.config import Config
    from app.core.snapshot import create_run, save_snapshot
    from app.db import init_db
    from app.models import ComparisonResult, DimensionDiff, ImplEvaluation, MetricDefinition

    db_path = tmp_path / "api-safe" / "caliber.sqlite3"
    conn = init_db(db_path)
    run_id = create_run(conn, ["edge"], str(tmp_path), {"abs": 0.0, "rel": 0.0})
    metric = MetricDefinition("edge", "极值", "qa", "d", "x", "day", [])
    diff = DimensionDiff(
        "d1", "value_mismatch",
        {"impl_a": math.inf, "impl_b": math.nan}, math.inf, "数值超出容差",
    )
    comparison = ComparisonResult(
        "edge", 1, ["impl_a", "impl_b"], "inconsistent", ["d1"], [diff],
        {"impl_count": 2, "dimension_count": 1, "consistent_dimensions": 0,
         "value_mismatches": 1, "missing_dimensions": 0, "diff_count": 1},
        0.0, 0.0,
    )
    evaluations = {
        "impl_a": ImplEvaluation("impl_a", "ds", "a.sql", {"d1": math.inf}, 1, 1.0),
        "impl_b": ImplEvaluation("impl_b", "ds", "b.sql", {"d1": math.nan}, 1, 1.0),
    }
    save_snapshot(conn, run_id, metric, comparison, evaluations)
    conn.close()

    app = create_app(Config(data_dir=tmp_path, db_path=db_path, testing=True))
    client = app.test_client()
    response = client.get(f"/api/reports/{run_id}?format=json")
    assert response.status_code == 200
    raw = response.get_data(as_text=True)
    # HTTP 出口必须是严格 JSON，不含裸 NaN/Infinity
    json.loads(raw, parse_constant=lambda c: (_ for _ in ()).throw(AssertionError(c)))
    assert '"Infinity"' in raw and '"NaN"' in raw


def test_web_pages_are_local_and_render(imported_client, app_config):
    home = imported_client.get("/")
    assert home.status_code == 200
    body = home.get_data(as_text=True)
    assert "指标准星" in body
    assert "http://" not in body and "https://" not in body

    metrics = imported_client.get("/metrics")
    assert metrics.status_code == 200
    assert "指标仓库" in metrics.get_data(as_text=True)

    detail = imported_client.get("/metrics/active_users")
    assert detail.status_code == 200
    assert "活跃用户数" in detail.get_data(as_text=True)

    runs = imported_client.get("/runs")
    assert runs.status_code == 200
    assert "多实现一致性比对" in runs.get_data(as_text=True)

    created = imported_client.post(
        "/api/runs",
        json={"metrics": ["active_users"], "datasets_dir": str(app_config.datasets_dir)},
    )
    run_id = created.get_json()["run"]["id"]
    report = imported_client.get(f"/reports/{run_id}")
    assert report.status_code == 200
    assert "指标一致性报告" in report.get_data(as_text=True)

    assert imported_client.get("/metrics/missing").status_code == 404
    assert imported_client.get("/reports/999").status_code == 404


def test_web_report_page_renders_non_finite_values(tmp_path: Path):
    import math

    from app import create_app
    from app.config import Config
    from app.core.snapshot import create_run, finish_run, save_snapshot
    from app.db import init_db
    from app.models import ComparisonResult, DimensionDiff, ImplEvaluation, MetricDefinition

    db_path = tmp_path / "web-safe" / "caliber.sqlite3"
    conn = init_db(db_path)
    run_id = create_run(conn, ["edge"], str(tmp_path), {"abs": 0.0, "rel": 0.0})
    metric = MetricDefinition("edge", "极值", "qa", "d", "x", "day", [])
    diff = DimensionDiff(
        "d1", "value_mismatch",
        {"impl_a": math.inf, "impl_b": -math.inf}, math.inf, "数值超出容差",
    )
    comparison = ComparisonResult(
        "edge", 1, ["impl_a", "impl_b"], "inconsistent", ["d1"], [diff],
        {"impl_count": 2, "dimension_count": 1, "consistent_dimensions": 0,
         "value_mismatches": 1, "missing_dimensions": 0, "diff_count": 1},
        0.0, 0.0,
    )
    evaluations = {
        "impl_a": ImplEvaluation("impl_a", "ds", "a.sql", {"d1": math.inf}, 1, 1.0),
        "impl_b": ImplEvaluation("impl_b", "ds", "b.sql", {"d1": -math.inf}, 1, 1.0),
    }
    save_snapshot(conn, run_id, metric, comparison, evaluations)
    finish_run(conn, run_id, status="completed",
               summary={"consistent": 0, "inconsistent": 1, "partial": 0,
                        "metrics": 1, "run_id": run_id})
    conn.close()

    app = create_app(Config(data_dir=tmp_path, db_path=db_path, testing=True))
    page = app.test_client().get(f"/reports/{run_id}")
    assert page.status_code == 200
    body = page.get_data(as_text=True)
    assert "Infinity" in body and "-Infinity" in body
    assert "inf</td>" not in body and "<td>inf" not in body
