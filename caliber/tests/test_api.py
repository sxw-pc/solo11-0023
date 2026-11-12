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
