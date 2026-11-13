"""Comparison runs, immutable snapshots and drift detection."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from ..audit import log_audit, record_error
from ..errors import CaliberError, NotFoundError, RunError
from ..models import ComparisonResult, ImplEvaluation, MetricDefinition, utc_now
from .evaluator import run_metric
from .versioning import get_latest, get_version, list_metrics, rollback_event_for_version

VERDICTS = ("consistent", "inconsistent", "partial")

# Traceability strategy recorded into every snapshot's execution metadata.
TRACEABILITY_STRATEGY = "frozen_definition_snapshot"
STRATEGY_RATIONALE = (
    "选择方案A（每次运行固化定义快照）：快照自带当时实际执行的指标定义与 SQL "
    "文本，回退、重复导入或磁盘 SQL 文件被覆写都不影响历史追溯；代价是每个快照"
    "重复存储定义与 SQL（定义与 SQL 体量小、离线运行频率低，存储成本可忽略），"
    "并需为旧快照提供按版本号读取 metric_versions 的降级路径。未选方案B（不可变"
    "版本标识）：数字版本与内容解耦虽省去重复存储，但追溯仍需回查 "
    "metric_versions，且无法覆盖 SQL 文件在磁盘上被覆写的情形。回退本身采用追加"
    "式实现（回退生成新版本号），版本号永不重新指向不同内容。"
)


def definition_hash(definition: dict[str, Any]) -> str:
    """Content hash of a metric definition (canonical JSON, sha256)."""
    canonical = json.dumps(definition, ensure_ascii=False, sort_keys=True)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_exec_meta(
    conn: sqlite3.Connection,
    key: str,
    version: int,
    definition: dict[str, Any],
    sql_records: dict[str, dict[str, str]],
) -> dict[str, Any]:
    """Execution metadata frozen into each snapshot.

    Records the traceability strategy choice (and its rationale) plus the
    content hashes needed to verify what exactly was executed.
    """
    rollback_event = rollback_event_for_version(conn, key, version)
    return {
        "traceability_strategy": TRACEABILITY_STRATEGY,
        "strategy_rationale": STRATEGY_RATIONALE,
        "definition_hash": definition_hash(definition),
        "definition_version": int(version),
        "definition_source": "metrics.current_version",
        "rollback": (
            {
                "event_id": rollback_event["id"],
                "kind": rollback_event["kind"],
                "from_version": rollback_event["from_version"],
                "to_version": rollback_event["to_version"],
            }
            if rollback_event
            else None
        ),
        "sql": {
            impl_id: {"sql_file": rec["sql_file"], "sha256": rec["sha256"]}
            for impl_id, rec in sorted(sql_records.items())
        },
        "captured_at": utc_now(),
    }


def _decode_snapshot(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["datasets"] = json.loads(item.pop("datasets_json"))
    item["impl_ids"] = json.loads(item.pop("impl_ids_json"))
    item["summary"] = json.loads(item.pop("summary_json"))
    item["result"] = json.loads(item.pop("result_json"))
    # Columns added by the traceability migration; NULL on legacy rows.
    for column, target in (
        ("definition_json", "frozen_definition"),
        ("sql_snapshot_json", "sql_snapshot"),
        ("exec_meta_json", "exec_meta"),
    ):
        raw = item.pop(column, None)
        item[target] = json.loads(raw) if raw else None
    return item


def create_run(conn: sqlite3.Connection, metrics: list[str], datasets_dir: str | Path,
               tolerances: dict[str, float]) -> int:
    cursor = conn.execute(
        "INSERT INTO runs(status, started_at, metrics_json, datasets_dir, tolerances_json) "
        "VALUES ('running', ?, ?, ?, ?)",
        (utc_now(), json.dumps(metrics, ensure_ascii=False), str(datasets_dir),
         json.dumps(tolerances, ensure_ascii=False)),
    )
    conn.commit()
    return int(cursor.lastrowid or 0)


def finish_run(conn: sqlite3.Connection, run_id: int, *, status: str,
               summary: dict[str, Any] | None = None, error: str | None = None) -> None:
    conn.execute(
        "UPDATE runs SET status = ?, finished_at = ?, summary_json = ?, error = ? "
        "WHERE id = ?",
        (status, utc_now(), json.dumps(summary or {}, ensure_ascii=False), error, run_id),
    )
    conn.commit()


def save_snapshot(conn: sqlite3.Connection, run_id: int, metric: MetricDefinition,
                  comparison: ComparisonResult, evaluations: dict[str, ImplEvaluation],
                  *, definition_snapshot: dict[str, Any] | None = None,
                  sql_snapshot: dict[str, dict[str, str]] | None = None,
                  exec_meta: dict[str, Any] | None = None) -> int:
    result = comparison.to_dict()
    result["metric"] = metric.to_dict()
    result["implementations"] = [item.to_dict() for item in evaluations.values()]
    datasets = sorted({item.source for item in evaluations.values()})
    frozen_definition = definition_snapshot if definition_snapshot is not None else None
    cursor = conn.execute(
        """
        INSERT INTO snapshots(run_id, metric_key, metric_version, datasets_json,
                              impl_ids_json, verdict, summary_json, result_json,
                              definition_json, definition_hash,
                              sql_snapshot_json, exec_meta_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id, metric.key, comparison.metric_version,
            json.dumps(datasets, ensure_ascii=False),
            json.dumps(comparison.impl_ids, ensure_ascii=False),
            comparison.verdict,
            json.dumps(comparison.summary, ensure_ascii=False),
            json.dumps(result, ensure_ascii=False, sort_keys=True),
            json.dumps(frozen_definition, ensure_ascii=False, sort_keys=True)
            if frozen_definition is not None else None,
            definition_hash(frozen_definition) if frozen_definition is not None else None,
            json.dumps(sql_snapshot, ensure_ascii=False, sort_keys=True)
            if sql_snapshot is not None else None,
            json.dumps(exec_meta, ensure_ascii=False, sort_keys=True)
            if exec_meta is not None else None,
            utc_now(),
        ),
    )
    conn.commit()
    return int(cursor.lastrowid or 0)


def list_snapshots(
    conn: sqlite3.Connection, metric_key: str | None = None, limit: int = 20
) -> list[dict[str, Any]]:
    if metric_key:
        rows = conn.execute(
            "SELECT * FROM snapshots WHERE metric_key = ? ORDER BY id DESC LIMIT ?",
            (metric_key, max(1, int(limit))),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM snapshots ORDER BY id DESC LIMIT ?", (max(1, int(limit)),)
        ).fetchall()
    return [_decode_snapshot(row) for row in rows]


def get_snapshot(conn: sqlite3.Connection, snapshot_id: int) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM snapshots WHERE id = ?", (snapshot_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"快照不存在: {snapshot_id}")
    return _decode_snapshot(row)


def snapshots_for_run(conn: sqlite3.Connection, run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM snapshots WHERE run_id = ? ORDER BY id", (run_id,)
    ).fetchall()
    return [_decode_snapshot(row) for row in rows]


def detect_drift(conn: sqlite3.Connection, metric_key: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, verdict, metric_version, created_at FROM snapshots "
        "WHERE metric_key = ? ORDER BY id",
        (metric_key,),
    ).fetchall()
    events: list[dict[str, Any]] = []
    for previous, current in zip(rows, rows[1:]):
        if previous["verdict"] == "consistent" and current["verdict"] == "inconsistent":
            events.append(
                {
                    "metric_key": metric_key,
                    "from_snapshot_id": previous["id"],
                    "to_snapshot_id": current["id"],
                    "from_version": previous["metric_version"],
                    "to_version": current["metric_version"],
                    "from_verdict": "consistent",
                    "to_verdict": "inconsistent",
                    "detected_at": current["created_at"],
                    "message": "判定从 consistent 漂移为 inconsistent",
                }
            )
    return events


def list_runs(conn: sqlite3.Connection, limit: int = 20) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (max(1, int(limit)),)
    ).fetchall()
    return [_decode_run(row) for row in rows]


def _decode_run(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["metrics"] = json.loads(item.pop("metrics_json"))
    item["tolerances"] = json.loads(item.pop("tolerances_json"))
    item["summary"] = json.loads(item.pop("summary_json"))
    return item


def get_run(conn: sqlite3.Connection, run_id: int) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM runs WHERE id = ?", (int(run_id),)).fetchone()
    if row is None:
        raise NotFoundError(f"运行不存在: {run_id}")
    return _decode_run(row)


def perform_run(
    conn: sqlite3.Connection,
    metric_keys: list[str] | None,
    catalog: Any,
    sql_roots: tuple[Path, ...] | list[Path],
    *,
    datasets_dir: str | Path,
    timeout_seconds: float = 5.0,
    max_rows: int = 10_000,
    abs_tolerance: float = 1e-6,
    rel_tolerance: float = 0.0,
) -> int:
    keys = list(dict.fromkeys(metric_keys or [item["key"] for item in list_metrics(conn)]))
    if not keys:
        raise RunError("没有可执行的指标，请先导入指标定义")
    tolerances = {"abs": float(abs_tolerance), "rel": float(rel_tolerance)}
    run_id = create_run(conn, keys, datasets_dir, tolerances)
    log_audit(conn, "run.started", f"run:{run_id}", "ok", {"metrics": keys})
    summary = {verdict: 0 for verdict in VERDICTS}
    try:
        for key in keys:
            latest = get_latest(conn, key)
            metric = MetricDefinition.from_dict(latest["definition"])
            comparison, evaluations, sql_records = run_metric(
                metric,
                int(latest["version"]),
                catalog,
                sql_roots,
                timeout_seconds=timeout_seconds,
                max_rows=max_rows,
                abs_tolerance=abs_tolerance,
                rel_tolerance=rel_tolerance,
            )
            exec_meta = build_exec_meta(
                conn, key, int(latest["version"]), latest["definition"], sql_records
            )
            save_snapshot(
                conn,
                run_id,
                metric,
                comparison,
                evaluations,
                definition_snapshot=latest["definition"],
                sql_snapshot=sql_records,
                exec_meta=exec_meta,
            )
            summary[comparison.verdict] = summary.get(comparison.verdict, 0) + 1
        summary["metrics"] = len(keys)
        summary["run_id"] = run_id
        finish_run(conn, run_id, status="completed", summary=summary)
        log_audit(conn, "run.completed", f"run:{run_id}", "ok", summary)
        return run_id
    except Exception as exc:
        finish_run(conn, run_id, status="failed", error=str(exc))
        record_error(conn, "run.failed", f"run:{run_id}", exc, extra={"metrics": keys})
        if isinstance(exc, CaliberError):
            raise
        raise RunError(f"比对运行失败: {exc}") from exc


def resolve_snapshot_trace(
    conn: sqlite3.Connection, snapshot: dict[str, Any]
) -> dict[str, Any]:
    """Resolve which definition/SQL a snapshot actually executed.

    New snapshots carry a frozen copy of the definition and SQL text, so they
    are self-contained.  Legacy snapshots only store the version number; for
    those we degrade to reading ``metric_versions`` by number and attach
    explicit notices instead of failing, so report and trace chains keep
    working.  This function never raises for missing content.
    """
    key = snapshot["metric_key"]
    version = int(snapshot["metric_version"])
    notices: list[str] = []
    frozen = snapshot.get("frozen_definition")
    sql_snapshot = snapshot.get("sql_snapshot")
    if frozen is not None:
        definition = frozen
        definition_source = "snapshot_frozen"
        degraded = False
        stored_hash = snapshot.get("definition_hash") or definition_hash(definition)
    else:
        degraded = True
        definition_source = "metric_versions_fallback"
        stored_hash = None
        notices.append(
            f"该快照为旧格式，未固化定义内容；定义按版本号 v{version} 从 "
            "metric_versions 读取。若此后发生过回退、重复导入或仓库迁移，"
            "读到的内容可能与当时实际执行的定义不一致。"
        )
        try:
            definition = get_version(conn, key, version)["definition"]
        except NotFoundError:
            definition = None
            definition_source = "unresolved"
            notices.append(
                f"metric_versions 中不存在 {key} v{version} 的版本记录，"
                "无法解析当时执行的定义内容。"
            )
    if sql_snapshot:
        sql_traced = True
    else:
        sql_traced = False
        if degraded:
            notices.append(
                "该快照未固化 SQL 文本，无法追溯当时实际执行的 SQL；"
                "磁盘上的 SQL 文件可能已被覆写。"
            )
    return {
        "snapshot_id": snapshot["id"],
        "run_id": snapshot["run_id"],
        "metric_key": key,
        "metric_version": version,
        "created_at": snapshot["created_at"],
        "degraded": degraded,
        "definition_source": definition_source,
        "notices": notices,
        "definition": definition,
        "definition_hash": stored_hash,
        "sql": sql_snapshot,
        "sql_traced": sql_traced,
        "exec_meta": snapshot.get("exec_meta"),
        "strategy": TRACEABILITY_STRATEGY,
    }


def trace_snapshot(conn: sqlite3.Connection, snapshot_id: int) -> dict[str, Any]:
    """Full traceability record for one snapshot (404 only if it does not exist)."""
    snapshot = get_snapshot(conn, snapshot_id)
    return resolve_snapshot_trace(conn, snapshot)


def trace_summary(trace: dict[str, Any]) -> dict[str, Any]:
    """Compact trace block embedded into reports."""
    return {
        "definition_source": trace["definition_source"],
        "degraded": trace["degraded"],
        "notices": trace["notices"],
        "definition_hash": trace["definition_hash"],
        "sql_traced": trace["sql_traced"],
    }
