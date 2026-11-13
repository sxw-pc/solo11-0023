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


def test_window_api_exposes_policy_and_incremental_recompute(imported_client, app_config):
    created = imported_client.post(
        "/api/runs",
        json={
            "metrics": ["active_users", "avg_order_value"],
            "datasets_dir": str(app_config.datasets_dir),
            "window": {"size": 3},
        },
    )
    assert created.status_code == 201
    payload = created.get_json()
    assert payload["run"]["summary"]["window"]["size"] == 3
    avg = next(
        item for item in payload["snapshots"] if item["metric_key"] == "avg_order_value"
    )
    assert avg["summary"]["window"]["status"] == "first_deviation"
    assert avg["summary"]["window"]["deviation_start_run_id"] == payload["run"]["id"]

    meta = imported_client.get("/api/windows/meta")
    assert meta.status_code == 200
    body = meta.get_json()
    assert body["default"]["policy"] == "run_count"
    assert body["default"]["selection_reason"]
    assert body["policies"] == ["run_count"]

    listing = imported_client.get("/api/windows?size=3")
    assert listing.status_code == 200
    data = listing.get_json()
    assert data["count"] == 2
    assert {item["status"] for item in data["windows"]} == {
        "consistent", "first_deviation"
    }

    recompute = imported_client.get("/api/windows/active_users?size=3&recompute=true")
    assert recompute.status_code == 200
    assert recompute.get_json()["window"]["changed"] is False

    detail = imported_client.get("/api/windows/avg_order_value?size=3")
    assert detail.get_json()["window"]["source"] == "snapshot"
    assert imported_client.get("/api/windows/unknown_metric").status_code == 404

    bad = imported_client.get("/api/windows?policy=time_span")
    assert bad.status_code == 400
    assert "WINDOW_POLICIES" in bad.get_json()["message"]
    assert imported_client.get("/api/windows?size=0").status_code == 400
    assert imported_client.get("/api/windows?recompute=maybe").status_code == 400


def test_window_report_endpoint_includes_window_block(imported_client, app_config):
    created = imported_client.post(
        "/api/runs",
        json={"metrics": ["active_users"], "datasets_dir": str(app_config.datasets_dir)},
    )
    run_id = created.get_json()["run"]["id"]
    response = imported_client.get(f"/api/reports/{run_id}?format=json")
    report = json.loads(response.get_data(as_text=True))
    assert report["window_policy"]["policy"] == "run_count"
    assert len(report["windows"]) == 1
    assert report["windows"][0]["metric_key"] == "active_users"
    assert report["windows"][0]["status"] == "consistent"
    assert report["summary"]["window_count"] == 1
    assert report["summary"]["window_consistent"] == 1

    page = imported_client.get(f"/reports/{run_id}")
    assert page.status_code == 200
    assert "归并窗口" in page.get_data(as_text=True)
