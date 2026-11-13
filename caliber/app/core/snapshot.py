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
from .versioning import get_latest, list_metrics
from .windowing import (
    DEFAULT_POLICY,
    DEFAULT_WINDOW_SIZE,
    execution_metadata,
    normalize_window,
    recompute_metric_window,
)

VERDICTS = ("consistent", "inconsistent", "partial")


def _decode_json(raw: Any, fallback: Any) -> Any:
    """Tolerant decode for legacy rows: never break the report chain."""
    if raw is None:
        return fallback
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return fallback


def _decode_snapshot(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["datasets"] = _decode_json(item.pop("datasets_json"), [])
    item["impl_ids"] = _decode_json(item.pop("impl_ids_json"), [])
    item["summary"] = _decode_json(item.pop("summary_json"), {})
    item["result"] = _decode_json(item.pop("result_json"), {})
    if not isinstance(item["datasets"], list):
        item["datasets"] = []
    if not isinstance(item["impl_ids"], list):
        item["impl_ids"] = []
    if not isinstance(item["summary"], dict):
        item["summary"] = {}
    if not isinstance(item["result"], dict):
        item["result"] = {}
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
                  *, window_policy: str = DEFAULT_POLICY,
                  window_size: int = DEFAULT_WINDOW_SIZE) -> int:
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
    snapshot_id = int(cursor.lastrowid or 0)
    # Incremental: only this metric's newest size+1 runs are read.  The window
    # block is written into this snapshot's summary_json (same table, no schema
    # change) so reports can export by window.
    recompute_metric_window(
        conn,
        metric.key,
        policy=window_policy,
        size=window_size,
        anchor_snapshot_id=snapshot_id,
    )
    return snapshot_id


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
    item["metrics"] = _decode_json(item.pop("metrics_json"), [])
    item["tolerances"] = _decode_json(item.pop("tolerances_json"), {})
    item["summary"] = _decode_json(item.pop("summary_json"), {})
    if not isinstance(item["summary"], dict):
        item["summary"] = {}
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
    window_policy: str = DEFAULT_POLICY,
    window_size: int = DEFAULT_WINDOW_SIZE,
) -> int:
    keys = list(dict.fromkeys(metric_keys or [item["key"] for item in list_metrics(conn)]))
    if not keys:
        raise RunError("没有可执行的指标，请先导入指标定义")
    window_policy, window_size = normalize_window(window_policy, window_size)
    tolerances = {"abs": float(abs_tolerance), "rel": float(rel_tolerance)}
    run_id = create_run(conn, keys, datasets_dir, tolerances)
    log_audit(conn, "run.started", f"run:{run_id}", "ok", {"metrics": keys})
    summary = {verdict: 0 for verdict in VERDICTS}
    try:
        for key in keys:
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
            )
            save_snapshot(
                conn, run_id, metric, comparison, evaluations,
                window_policy=window_policy, window_size=window_size,
            )
            summary[comparison.verdict] = summary.get(comparison.verdict, 0) + 1
        summary["metrics"] = len(keys)
        summary["run_id"] = run_id
        summary["window"] = execution_metadata(window_policy, window_size)
        finish_run(conn, run_id, status="completed", summary=summary)
        log_audit(conn, "run.completed", f"run:{run_id}", "ok", summary)
        return run_id
    except Exception as exc:
        finish_run(conn, run_id, status="failed", error=str(exc))
        record_error(conn, "run.failed", f"run:{run_id}", exc, extra={"metrics": keys})
        if isinstance(exc, CaliberError):
            raise
        raise RunError(f"比对运行失败: {exc}") from exc
