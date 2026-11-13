"""版本回退、快照固化与追溯的验收测试。

覆盖用户验收路径：跑过多次且中途改过定义和 SQL 的指标 → 回退版本 →
新 run 用回退后定义、历史 run 报告不变 → 撤销回退定义恢复 →
老快照走追溯出现降级提示且能定位当时版本。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core import versioning
from app.core.dataset_loader import DatasetCatalog
from app.core.dsl_parser import parse_metric_file
from app.core.registry import MetricRegistry
from app.core.reporter import build_report, export_report
from app.core.snapshot import (
    perform_run,
    snapshots_for_run,
    trace_snapshot,
)
from app.errors import CaliberError, NotFoundError

KEY = "active_users"
ORIGINAL_NAME = "活跃用户数"
REVISED_NAME = "活跃用户数（改版）"

REVISED_SQL = (
    "-- 改版 SQL：不再过滤 status\n"
    "SELECT last_active_date AS day, COUNT(DISTINCT user_id) AS active_users\n"
    "FROM users_a\n"
    "GROUP BY last_active_date\n"
    "ORDER BY day\n"
)


def _import_active_users(conn, fixture_tree: Path, sql_roots) -> MetricRegistry:
    registry = MetricRegistry(conn, sql_roots)
    registry.import_file(fixture_tree / "metrics" / f"{KEY}.yaml")
    return registry


def _revise_definition_and_sql(registry, fixture_tree: Path) -> None:
    """模拟「中途改过定义和 SQL」：定义改名改描述（v2），impl_a SQL 去掉过滤。"""
    metric = parse_metric_file(fixture_tree / "metrics" / f"{KEY}.yaml")
    metric.name = REVISED_NAME
    metric.description = "改版口径：不再过滤 status。"
    registry.import_definition(metric, "test-revision")
    (fixture_tree / "impls" / f"{KEY}_impl_a.sql").write_text(
        REVISED_SQL, encoding="utf-8"
    )


def _run(conn, catalog, fixture_tree: Path, sql_roots) -> int:
    return perform_run(
        conn,
        [KEY],
        catalog,
        sql_roots,
        datasets_dir=fixture_tree / "datasets",
        timeout_seconds=2.0,
        max_rows=100,
        abs_tolerance=1e-6,
        rel_tolerance=0.0,
    )


@pytest.fixture
def metric_with_history(conn, fixture_tree: Path, sql_roots, catalog):
    """v1 定义跑一轮 → 改定义+SQL 成 v2 再跑一轮。返回 (registry, run1, run2)。"""
    registry = _import_active_users(conn, fixture_tree, sql_roots)
    run1 = _run(conn, catalog, fixture_tree, sql_roots)
    _revise_definition_and_sql(registry, fixture_tree)
    run2 = _run(conn, catalog, fixture_tree, sql_roots)
    return registry, run1, run2


def test_acceptance_rollback_run_and_undo(
    conn, fixture_tree: Path, sql_roots, catalog, metric_with_history
):
    _, run1, run2 = metric_with_history
    snap1 = snapshots_for_run(conn, run1)[0]
    snap2 = snapshots_for_run(conn, run2)[0]
    # v2 的 SQL 去掉了 status 过滤：2026-02-04 只有非活跃用户，成为 impl_a
    # 独有维度，判定从 consistent 变为 partial，证明 SQL 改动确实生效
    assert (snap1["metric_version"], snap1["verdict"]) == (1, "consistent")
    assert (snap2["metric_version"], snap2["verdict"]) == (2, "partial")
    # 每次运行固化了当时实际执行的定义与 SQL
    assert snap1["frozen_definition"]["name"] == ORIGINAL_NAME
    assert snap2["frozen_definition"]["name"] == REVISED_NAME
    assert snap2["sql_snapshot"]["impl_a"]["content"] == REVISED_SQL
    assert snap1["sql_snapshot"]["impl_a"]["content"] != REVISED_SQL
    # 执行元信息写入了策略选择与理由
    meta = snap2["exec_meta"]
    assert meta["traceability_strategy"] == "frozen_definition_snapshot"
    assert "方案A" in meta["strategy_rationale"]
    assert meta["definition_version"] == 2
    assert meta["rollback"] is None

    # 回退到 v1：追加式生成 v3，历史版本行不被改写
    result = versioning.rollback_to_version(conn, KEY, 1, "验收：回到旧口径")
    event = result["event"]
    assert (event["from_version"], event["to_version"], event["new_version"]) == (2, 1, 3)
    assert result["metric"]["version"] == 3
    assert result["metric"]["definition"]["name"] == ORIGINAL_NAME
    assert versioning.get_version(conn, KEY, 2)["definition"]["name"] == REVISED_NAME

    # 回退后新发起的 run 必须使用回退后的定义
    run3 = _run(conn, catalog, fixture_tree, sql_roots)
    snap3 = snapshots_for_run(conn, run3)[0]
    assert snap3["metric_version"] == 3
    assert snap3["frozen_definition"]["name"] == ORIGINAL_NAME
    assert snap3["frozen_definition"] == versioning.get_version(conn, KEY, 1)["definition"]
    # SQL 以运行时磁盘内容为准并固化（回退恢复的是定义，不是磁盘文件）
    assert snap3["sql_snapshot"]["impl_a"]["content"] == REVISED_SQL
    assert snap3["exec_meta"]["rollback"]["event_id"] == event["id"]
    assert snap3["exec_meta"]["rollback"]["to_version"] == 1

    # 历史 run 的报告内容不受回退影响
    report1 = build_report(conn, run1)
    report2 = build_report(conn, run2)
    metric1 = report1["metrics"][0]
    metric2 = report2["metrics"][0]
    assert (metric1["version"], metric1["name"], metric1["verdict"]) == (
        1, ORIGINAL_NAME, "consistent")
    assert (metric2["version"], metric2["name"], metric2["verdict"]) == (
        2, REVISED_NAME, "partial")
    assert report1["metrics"][0]["trace"]["degraded"] is False

    # 撤销回退：定义恢复到回退前的 v2 内容（追加式生成 v4）
    undone = versioning.undo_rollback(conn, KEY)
    undo_event = undone["event"]
    assert undo_event["kind"] == "undo"
    assert (undo_event["from_version"], undo_event["to_version"]) == (3, 2)
    latest = versioning.get_latest(conn, KEY)
    assert latest["version"] == 4
    assert latest["definition"]["name"] == REVISED_NAME
    assert latest["definition"] == versioning.get_version(conn, KEY, 2)["definition"]

    # 回退事件被标记撤销，审计完整
    events = versioning.list_rollbacks(conn, KEY)
    assert [item["kind"] for item in events] == ["undo", "rollback"]
    assert events[1]["undone_by"] == events[0]["id"]
    assert events[1]["undone_at"]
    actions = {
        row[0]
        for row in conn.execute(
            "SELECT action FROM audit_log WHERE entity = ?", (KEY,)
        )
    }
    assert {"metric.rollback", "metric.rollback_undo"} <= actions


def test_rollback_is_atomic_when_audit_insert_fails(
    conn, fixture_tree: Path, sql_roots, monkeypatch
):
    registry = _import_active_users(conn, fixture_tree, sql_roots)
    metric = parse_metric_file(fixture_tree / "metrics" / f"{KEY}.yaml")
    metric.name = REVISED_NAME
    registry.import_definition(metric)
    before_versions = conn.execute(
        "SELECT COUNT(*) FROM metric_versions WHERE metric_key = ?", (KEY,)
    ).fetchone()[0]

    def boom(*args, **kwargs):
        raise RuntimeError("模拟回退中途失败")

    monkeypatch.setattr("app.core.versioning.insert_audit_row", boom)
    with pytest.raises(RuntimeError):
        versioning.rollback_to_version(conn, KEY, 1)
    # 定义表、版本表、回退事件表全部保持原状，没有被改坏
    row = conn.execute(
        "SELECT current_version, definition_json FROM metrics WHERE key = ?", (KEY,)
    ).fetchone()
    assert row["current_version"] == 2
    assert json.loads(row["definition_json"])["name"] == REVISED_NAME
    assert conn.execute(
        "SELECT COUNT(*) FROM metric_versions WHERE metric_key = ?", (KEY,)
    ).fetchone()[0] == before_versions
    assert conn.execute("SELECT COUNT(*) FROM metric_rollbacks").fetchone()[0] == 0
    # 失败本身也留下了错误审计
    assert conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE action = 'metric.rollback' "
        "AND status = 'error'"
    ).fetchone()[0] == 1


def test_rollback_validation_errors(conn, fixture_tree: Path, sql_roots):
    _import_active_users(conn, fixture_tree, sql_roots)
    with pytest.raises(NotFoundError):
        versioning.rollback_to_version(conn, KEY, 99)
    with pytest.raises(CaliberError) as noop:
        versioning.rollback_to_version(conn, KEY, 1)  # 已是当前版本
    assert noop.value.code == "rollback_noop"
    with pytest.raises(NotFoundError):
        versioning.rollback_to_version(conn, "missing_metric", 1)
    with pytest.raises(CaliberError) as nothing:
        versioning.undo_rollback(conn, KEY)  # 没有可撤销的回退
    assert nothing.value.code == "rollback_nothing_to_undo"
    with pytest.raises(NotFoundError):
        versioning.undo_rollback(conn, "missing_metric")
    # 失败的回退不产生任何残留
    assert conn.execute("SELECT COUNT(*) FROM metric_rollbacks").fetchone()[0] == 0
    assert versioning.get_latest(conn, KEY)["version"] == 1


def _make_legacy_snapshot(conn, run_id: int) -> int:
    """把某次运行的快照降级为旧格式（无固化内容），模拟迁移前的数据。"""
    snapshot_id = snapshots_for_run(conn, run_id)[0]["id"]
    conn.execute(
        "UPDATE snapshots SET definition_json = NULL, definition_hash = NULL, "
        "sql_snapshot_json = NULL, exec_meta_json = NULL WHERE id = ?",
        (snapshot_id,),
    )
    conn.commit()
    return snapshot_id


def test_legacy_snapshot_trace_degrades_with_notice(
    conn, fixture_tree: Path, sql_roots, catalog, metric_with_history
):
    _, run1, _ = metric_with_history
    snapshot_id = _make_legacy_snapshot(conn, run1)

    trace = trace_snapshot(conn, snapshot_id)
    assert trace["degraded"] is True
    assert trace["definition_source"] == "metric_versions_fallback"
    assert trace["sql_traced"] is False
    assert trace["sql"] is None
    # 能定位到当时版本，且定义按版本号从 metric_versions 解析
    assert trace["metric_version"] == 1
    assert trace["definition"] == versioning.get_version(conn, KEY, 1)["definition"]
    notice_text = "；".join(trace["notices"])
    assert "旧格式" in notice_text
    assert "v1" in notice_text
    assert "metric_versions" in notice_text
    assert "SQL" in notice_text

    # 报告链不中断：降级快照照常出报告，并带显式提示
    report = build_report(conn, run1)
    assert report["summary"]["degraded_snapshot_count"] == 1
    metric = report["metrics"][0]
    assert metric["trace"]["degraded"] is True
    assert metric["trace"]["definition_source"] == "metric_versions_fallback"
    assert metric["verdict"] == "consistent"
    fmt, markdown = export_report(report, "md")
    assert fmt == "md"
    assert "定义追溯" in markdown
    assert "旧格式" in markdown
    fmt, rendered = export_report(report, "html")
    assert "定义追溯" in rendered


def test_legacy_snapshot_with_missing_version_record_still_traces(
    conn, fixture_tree: Path, sql_roots, catalog, metric_with_history
):
    _, run1, _ = metric_with_history
    snapshot_id = _make_legacy_snapshot(conn, run1)
    conn.execute(
        "UPDATE snapshots SET metric_version = 99 WHERE id = ?", (snapshot_id,)
    )
    conn.commit()
    trace = trace_snapshot(conn, snapshot_id)
    assert trace["degraded"] is True
    assert trace["definition_source"] == "unresolved"
    assert trace["definition"] is None
    assert any("不存在" in notice for notice in trace["notices"])
    # 报告链依然不中断
    report = build_report(conn, run1)
    assert report["metrics"][0]["trace"]["degraded"] is True


def test_frozen_snapshot_trace_is_self_contained(
    conn, fixture_tree: Path, sql_roots, catalog, metric_with_history
):
    _, _, run2 = metric_with_history
    snap2 = snapshots_for_run(conn, run2)[0]
    trace = trace_snapshot(conn, snap2["id"])
    assert trace["degraded"] is False
    assert trace["definition_source"] == "snapshot_frozen"
    assert trace["notices"] == []
    assert trace["definition"]["name"] == REVISED_NAME
    assert trace["definition_hash"].startswith("sha256:")
    assert trace["sql_traced"] is True
    assert trace["sql"]["impl_a"]["content"] == REVISED_SQL
    assert trace["exec_meta"]["traceability_strategy"] == "frozen_definition_snapshot"
    # 即使磁盘 SQL 之后又被覆写，快照里的 SQL 仍然是当时执行的文本
    (fixture_tree / "impls" / f"{KEY}_impl_a.sql").write_text(
        "SELECT 1\n", encoding="utf-8"
    )
    again = trace_snapshot(conn, snap2["id"])
    assert again["sql"]["impl_a"]["content"] == REVISED_SQL


def test_rollback_api_flow(imported_client, app_config):
    # 再导入一次制造 v2
    from app.core.registry import MetricRegistry
    from app.db import init_db

    connection = init_db(app_config.db_path)
    try:
        MetricRegistry(connection, app_config.sql_roots).import_directory(
            app_config.metrics_dir
        )
    finally:
        connection.close()

    response = imported_client.post(
        "/api/metrics/active_users/rollback",
        json={"version": 1, "reason": "api 验收"},
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["event"]["new_version"] == 3
    assert payload["metric"]["version"] == 3

    detail = imported_client.get("/api/metrics/active_users").get_json()
    assert detail["active_rollback"]["to_version"] == 1

    versions = imported_client.get("/api/metrics/active_users/versions").get_json()
    origin_by_version = {item["version"]: item["origin"] for item in versions["versions"]}
    assert origin_by_version[3] == "rollback"
    assert origin_by_version[1] is None

    rollbacks = imported_client.get("/api/metrics/active_users/rollbacks").get_json()
    assert rollbacks["count"] == 1

    undo = imported_client.post("/api/metrics/active_users/rollback/undo", json={})
    assert undo.status_code == 200
    assert undo.get_json()["metric"]["version"] == 4

    again = imported_client.post("/api/metrics/active_users/rollback/undo", json={})
    assert again.status_code == 400
    assert again.get_json()["error"] == "rollback_nothing_to_undo"


def test_rollback_api_validation(imported_client):
    missing = imported_client.post("/api/metrics/active_users/rollback", json={})
    assert missing.status_code == 400

    bad_version = imported_client.post(
        "/api/metrics/active_users/rollback", json={"version": "abc"}
    )
    assert bad_version.status_code == 400

    not_found = imported_client.post(
        "/api/metrics/active_users/rollback", json={"version": 42}
    )
    assert not_found.status_code == 404

    unknown_metric = imported_client.post(
        "/api/metrics/unknown_metric/rollback", json={"version": 1}
    )
    assert unknown_metric.status_code == 404


def test_snapshot_trace_api(imported_client, app_config):
    created = imported_client.post(
        "/api/runs",
        json={
            "metrics": ["active_users"],
            "datasets_dir": str(app_config.datasets_dir),
        },
    )
    assert created.status_code == 201
    snapshot_id = created.get_json()["snapshots"][0]["id"]

    response = imported_client.get(f"/api/snapshots/{snapshot_id}/trace")
    assert response.status_code == 200
    trace = response.get_json()["trace"]
    assert trace["degraded"] is False
    assert trace["definition"]["key"] == "active_users"
    assert trace["sql_traced"] is True

    detail = imported_client.get(f"/api/snapshots/{snapshot_id}")
    assert detail.status_code == 200
    assert detail.get_json()["snapshot"]["exec_meta"]["traceability_strategy"]

    assert imported_client.get("/api/snapshots/999/trace").status_code == 404


def test_existing_database_migrates_and_legacy_rows_degrade(
    tmp_path: Path, fixture_tree: Path, sql_roots, catalog
):
    """老库（无固化列、无回退表）经 init_db 迁移后：旧快照按版本号降级读取。"""
    import sqlite3

    from app.db import init_db

    db_path = tmp_path / "legacy.db"
    # 手工建一个迁移前 schema 的老库，并写入一条只有版本号的旧快照
    legacy = sqlite3.connect(db_path)
    legacy.row_factory = sqlite3.Row
    legacy.executescript(
        """
        CREATE TABLE metrics (
            key TEXT PRIMARY KEY, name TEXT NOT NULL, owner TEXT NOT NULL,
            unit TEXT NOT NULL, grain TEXT NOT NULL, current_version INTEGER NOT NULL,
            definition_json TEXT NOT NULL, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE metric_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            metric_key TEXT NOT NULL, version INTEGER NOT NULL,
            definition_json TEXT NOT NULL, source_file TEXT NOT NULL,
            created_at TEXT NOT NULL, UNIQUE(metric_key, version)
        );
        CREATE TABLE runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, status TEXT NOT NULL,
            started_at TEXT NOT NULL, finished_at TEXT, metrics_json TEXT NOT NULL,
            datasets_dir TEXT NOT NULL, tolerances_json TEXT NOT NULL,
            summary_json TEXT NOT NULL DEFAULT '{}', error TEXT
        );
        CREATE TABLE snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL,
            metric_key TEXT NOT NULL, metric_version INTEGER NOT NULL,
            datasets_json TEXT NOT NULL, impl_ids_json TEXT NOT NULL,
            verdict TEXT NOT NULL, summary_json TEXT NOT NULL,
            result_json TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
            action TEXT NOT NULL, entity TEXT NOT NULL, status TEXT NOT NULL,
            detail_json TEXT NOT NULL DEFAULT '{}'
        );
        """
    )
    legacy.execute(
        "INSERT INTO runs(status, started_at, metrics_json, datasets_dir, "
        "tolerances_json) VALUES ('completed', '2026-10-30T08:00:00+00:00', "
        "'[\"active_users\"]', 'fixtures/datasets', '{\"abs\": 1e-06, \"rel\": 0.0}')"
    )
    legacy.execute(
        "INSERT INTO snapshots(run_id, metric_key, metric_version, datasets_json, "
        "impl_ids_json, verdict, summary_json, result_json, created_at) VALUES "
        "(1, 'active_users', 1, '[\"users_a\"]', '[\"impl_a\"]', 'consistent', "
        "'{}', '{\"metric\": {\"key\": \"active_users\"}}', "
        "'2026-10-30T08:00:01+00:00')"
    )
    legacy.commit()
    legacy.close()

    # 用新代码打开老库：迁移应补齐新列与新表，旧行内容保持 NULL
    conn = init_db(db_path)
    try:
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(snapshots)")
        }
        assert {"definition_json", "definition_hash", "sql_snapshot_json",
                "exec_meta_json"} <= columns
        conn.execute("SELECT COUNT(*) FROM metric_rollbacks").fetchone()

        # 老库补导入 v1 定义后，旧快照可按版本号降级解析
        MetricRegistry(conn, sql_roots).import_file(
            fixture_tree / "metrics" / f"{KEY}.yaml"
        )
        trace = trace_snapshot(conn, 1)
        assert trace["degraded"] is True
        assert trace["definition_source"] == "metric_versions_fallback"
        assert trace["metric_version"] == 1
        assert trace["definition"]["key"] == KEY
        assert trace["notices"]
        # 迁移后新发起的运行照常固化定义快照
        run_id = _run(conn, catalog, fixture_tree, sql_roots)
        snap = snapshots_for_run(conn, run_id)[0]
        assert snap["frozen_definition"]["key"] == KEY
        assert snap["exec_meta"]["traceability_strategy"]
    finally:
        conn.close()
