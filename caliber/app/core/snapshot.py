"""Comparison runs, immutable snapshots and drift detection."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from ..audit import log_audit, record_error
from ..errors import CaliberError, NotFoundError, RunError
from ..models import ComparisonResult, ImplEvaluation, MetricDefinition, utc_now
from .evaluator import run_metric
from .sql_gate import GATE_META, SqlGate
from .versioning import get_latest, list_metrics

VERDICTS = ("consistent", "inconsistent", "partial", "rejected")


def _decode_snapshot(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["datasets"] = json.loads(item.pop("datasets_json"))
    item["impl_ids"] = json.loads(item.pop("impl_ids_json"))
    item["summary"] = json.loads(item.pop("summary_json"))
    item["result"] = json.loads(item.pop("result_json"))
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
                  comparison: ComparisonResult, evaluations: dict[str, ImplEvaluation]) -> int:
    result = comparison.to_dict()
    result["metric"] = metric.to_dict()
    result["implementations"] = [item.to_dict() for item in evaluations.values()]
    datasets = sorted({item.source for item in evaluations.values()})
    cursor = conn.execute(
        """
        INSERT INTO snapshots(run_id, metric_key, metric_version, datasets_json,
                              impl_ids_json, verdict, summary_json, result_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id, metric.key, comparison.metric_version,
            json.dumps(datasets, ensure_ascii=False),
            json.dumps(comparison.impl_ids, ensure_ascii=False),
            comparison.verdict,
            json.dumps(comparison.summary, ensure_ascii=False),
            json.dumps(result, ensure_ascii=False, sort_keys=True),
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
    # 请求的指标不存在属于前置参数错误，运行开始前直接失败（保持 404 语义）；
    # 门禁拒绝与单指标运行期异常才在下面按实现/指标隔离，不中断整批。
    available = {item["key"] for item in list_metrics(conn)}
    missing = [key for key in keys if key not in available]
    if missing:
        raise NotFoundError(f"指标不存在: {', '.join(missing)}")
    tolerances = {"abs": float(abs_tolerance), "rel": float(rel_tolerance)}
    run_id = create_run(conn, keys, datasets_dir, tolerances)
    log_audit(conn, "run.started", f"run:{run_id}", "ok", {"metrics": keys})
    summary = {verdict: 0 for verdict in VERDICTS}
    # 运行侧完整模式门禁：复用 import 侧同一个 SqlGate 模块，
    # 内容哈希缓存保证文件被替换时在执行前被重新拦下。
    gate = SqlGate(catalog.readonly_connection())
    failed_metrics: list[dict[str, str]] = []
    try:
        for key in keys:
            try:
                latest = get_latest(conn, key)
                metric = MetricDefinition.from_dict(latest["definition"])
                comparison, evaluations = run_metric(
                    metric,
                    int(latest["version"]),
                    catalog,
                    sql_roots,
                    timeout_seconds=timeout_seconds,
                    max_rows=max_rows,
                    abs_tolerance=abs_tolerance,
                    rel_tolerance=rel_tolerance,
                    gate=gate,
                    audit_conn=conn,
                )
                save_snapshot(conn, run_id, metric, comparison, evaluations)
                summary[comparison.verdict] = summary.get(comparison.verdict, 0) + 1
                rejected = [
                    {"impl_id": item.impl_id, "rule": (item.rejection_reason or {}).get("rule")}
                    for item in evaluations.values()
                    if item.rejected
                ]
                if rejected:
                    summary.setdefault("rejected_impls", []).extend(
                        {"metric_key": key, **item} for item in rejected
                    )
            except Exception as exc:
                # 单指标失败不允许中断整批 run，也不回滚已入库的合法版本。
                record_error(
                    conn, "run.metric_failed", f"run:{run_id}", exc,
                    extra={"metric_key": key},
                )
                failed_metrics.append({"metric_key": key, "error": str(exc)})
        summary["metrics"] = len(keys)
        summary["run_id"] = run_id
        summary["failed_metrics"] = failed_metrics
        summary["gate_meta"] = GATE_META
        finish_run(conn, run_id, status="completed", summary=summary)
        log_audit(conn, "run.completed", f"run:{run_id}", "ok", summary)
        return run_id
    except Exception as exc:
        finish_run(conn, run_id, status="failed", error=str(exc))
        record_error(conn, "run.failed", f"run:{run_id}", exc, extra={"metrics": keys})
        if isinstance(exc, CaliberError):
            raise
        raise RunError(f"比对运行失败: {exc}") from exc
