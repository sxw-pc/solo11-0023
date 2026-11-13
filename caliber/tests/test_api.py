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


def test_run_accepts_max_workers_and_returns_per_rule_items(imported_client, app_config):
    response = imported_client.post(
        "/api/runs",
        json={
            "metrics": ["active_users", "conversion_rate", "avg_order_value"],
            "datasets_dir": str(app_config.datasets_dir),
            "max_workers": 2,
        },
    )
    assert response.status_code == 201
    payload = response.get_json()
    assert payload["run"]["summary"]["max_workers"] == 2
    assert payload["run"]["summary"]["peak_workers"] <= 2
    items = payload["items"]
    assert [item["position"] for item in items] == [0, 1, 2]
    assert all(item["status"] == "success" for item in items)
    assert {item["worker"] for item in items}  # workers were recorded
    # Detail endpoint also exposes the per-rule items.
    detail = imported_client.get(f"/api/runs/{payload['run']['id']}")
    assert len(detail.get_json()["items"]) == 3


def test_run_rejects_invalid_max_workers(imported_client, app_config):
    response = imported_client.post(
        "/api/runs",
        json={
            "metrics": ["active_users"],
            "datasets_dir": str(app_config.datasets_dir),
            "max_workers": 0,
        },
    )
    assert response.status_code == 400
    response = imported_client.post(
        "/api/runs",
        json={
            "metrics": ["active_users"],
            "datasets_dir": str(app_config.datasets_dir),
            "max_workers": "many",
        },
    )
    assert response.status_code == 400


def test_run_resume_skips_succeeded_rules(imported_client, app_config, monkeypatch):
    from app.core import snapshot as snapshot_module

    real_run_metric = snapshot_module.run_metric

    def flaky(metric, version, catalog, roots, **kwargs):
        if metric.key == "avg_order_value" and not flaky.failed:
            flaky.failed = True
            from app.errors import EvaluationError

            raise EvaluationError("首轮注入失败")
        return real_run_metric(metric, version, catalog, roots, **kwargs)

    flaky.failed = False
    monkeypatch.setattr(snapshot_module, "run_metric", flaky)

    first = imported_client.post(
        "/api/runs",
        json={
            "metrics": ["active_users", "avg_order_value"],
            "datasets_dir": str(app_config.datasets_dir),
            "max_workers": 2,
        },
    ).get_json()
    first_items = {item["metric_key"]: item for item in first["items"]}
    assert first_items["avg_order_value"]["status"] == "failed"
    assert first["run"]["status"] == "completed_with_errors"

    second = imported_client.post(
        "/api/runs",
        json={
            "metrics": ["active_users", "avg_order_value"],
            "datasets_dir": str(app_config.datasets_dir),
            "max_workers": 2,
            "resume": True,
        },
    ).get_json()
    second_items = {item["metric_key"]: item for item in second["items"]}
    assert second_items["active_users"]["status"] == "skipped"
    assert second_items["avg_order_value"]["status"] == "success"
    assert second["run"]["status"] == "completed"
    assert second["run"]["summary"]["skipped"] == 1


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
