"""HTTP API tests for retention dry-run and pruning endpoints."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from app.db import connect


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def _seed_old_run(db_path, run_id: int, days_old: int, *, snapshots: int = 1) -> None:
    conn = connect(db_path)
    old = datetime.now(timezone.utc) - timedelta(days=days_old)
    conn.execute(
        "INSERT INTO runs(id,status,started_at,finished_at,metrics_json,"
        "datasets_dir,tolerances_json) VALUES (?,?,?,?,?,?,?)",
        (run_id, "completed", _iso(old), _iso(old), '["m"]', "/tmp", "{}"),
    )
    for index in range(snapshots):
        conn.execute(
            "INSERT INTO snapshots(run_id,metric_key,metric_version,datasets_json,"
            "impl_ids_json,verdict,summary_json,result_json,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (run_id, f"m{index}", 1, "[]", "[]", "consistent", "{}", "{}", _iso(old)),
        )
    conn.execute(
        "INSERT INTO audit_log(created_at,action,entity,status,detail_json) "
        "VALUES (?,?,?,?,?)",
        (_iso(old), "run.completed", f"run:{run_id}", "ok", "{}"),
    )
    conn.commit()
    conn.close()


def test_plan_endpoint_is_read_only(imported_client, app_config):
    _seed_old_run(app_config.db_path, 1, 120, snapshots=2)
    before = {}
    conn = connect(app_config.db_path)
    for table in ("runs", "snapshots", "audit_log"):
        before[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    conn.close()

    response = imported_client.post("/api/retention/plan", json={"retention_days": 90})
    assert response.status_code == 200
    plan = response.get_json()["plan"]
    assert plan["dry_run"] is True
    assert plan["eligible_runs"] == 1
    assert plan["snapshots"] == 2
    assert plan["run_audit_records"] == 1

    conn = connect(app_config.db_path)
    after = {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("runs", "snapshots", "audit_log")
    }
    conn.close()
    assert after == before


def test_prune_endpoint_deletes_and_audits(imported_client, app_config):
    _seed_old_run(app_config.db_path, 1, 120)
    _seed_old_run(app_config.db_path, 2, 5)  # recent, must survive

    response = imported_client.post(
        "/api/retention/prune", json={"retention_days": 90, "batch_size": 1}
    )
    assert response.status_code == 200
    result = response.get_json()["result"]
    assert result["dry_run"] is False
    assert result["pruned_run_ids"] == [1]

    conn = connect(app_config.db_path)
    remaining = [row[0] for row in conn.execute("SELECT id FROM runs")]
    assert remaining == [2]
    orphans = conn.execute(
        "SELECT COUNT(*) FROM snapshots s LEFT JOIN runs r ON r.id=s.run_id "
        "WHERE r.id IS NULL"
    ).fetchone()[0]
    assert orphans == 0
    audit = conn.execute(
        "SELECT action, entity, status FROM audit_log WHERE action='retention.prune'"
    ).fetchall()
    assert len(audit) == 1 and audit[0]["entity"] == "retention"
    conn.close()


def test_prune_uses_configured_default_window(imported_client, app_config):
    app_config.retention_days = 90
    _seed_old_run(app_config.db_path, 1, 120)
    # No retention_days in payload -> falls back to config value.
    response = imported_client.post("/api/retention/prune", json={})
    assert response.status_code == 200
    assert response.get_json()["result"]["pruned_run_ids"] == [1]


def test_invalid_retention_arguments_rejected(imported_client):
    for payload in (
        {"retention_days": 0},
        {"retention_days": -7},
        {"retention_days": "soon"},
        {"retention_days": 1.5},
        {"retention_days": 30, "batch_size": 0},
    ):
        response = imported_client.post("/api/retention/plan", json=payload)
        assert response.status_code == 400, payload
        assert response.get_json()["error"] == "caliber_error"


def test_retention_page_renders(imported_client):
    response = imported_client.get("/retention")
    assert response.status_code == 200
    assert "留存裁剪" in response.get_data(as_text=True)


def test_prune_busy_conflict_returns_structured_400(
    imported_client, app_config, monkeypatch
):
    from app.core import retention
    from app.errors import CaliberError

    _seed_old_run(app_config.db_path, 1, 120)

    class Busy(CaliberError):
        code = "retention_busy"

    def raise_busy(*args, **kwargs):
        raise Busy("裁剪期间检测到持续并发写入")

    monkeypatch.setattr(retention, "prune", raise_busy)
    response = imported_client.post(
        "/api/retention/prune", json={"retention_days": 90}
    )
    assert response.status_code == 400
    assert response.get_json()["error"] == "retention_busy"


def test_report_page_for_pruned_run_shows_hint_not_500(imported_client, app_config):
    _seed_old_run(app_config.db_path, 7, 120)
    imported_client.post("/api/retention/prune", json={"retention_days": 90})

    # HTML page: friendly 404 hint, never a 500.
    page = imported_client.get("/reports/7")
    assert page.status_code == 404
    body = page.get_data(as_text=True)
    assert "报告无法读取" in body or "已不存在" in body

    # JSON API still returns the structured not_found envelope.
    api = imported_client.get("/api/reports/7")
    assert api.status_code == 404
    assert api.get_json()["error"] == "not_found"

    # Surviving runs' reports still render normally.
    _seed_old_run(app_config.db_path, 8, 1)
    ok = imported_client.get("/reports/8?format=json")
    # report page route (HTML) for a run without snapshots still builds.
    html_ok = imported_client.get("/reports/8")
    assert html_ok.status_code == 200
    assert ok.status_code == 200
