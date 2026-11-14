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

VERDICTS = ("consistent", "inconsistent", "partial", "unevaluable")
DATASET_REF_FIELDS = ("key", "name", "rel_path")


def normalize_dataset_refs(raw: Any) -> list[dict[str, str]]:
    """Normalize the dataset list stored in a snapshot.

    New snapshots persist reference objects (key/name/rel_path). Snapshots
    written by older versions only stored bare file-name strings; those are
    upgraded on read into reference objects with a readable note instead of
    failing.
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [raw]
    refs: list[dict[str, str]] = []
    legacy = False
    for item in raw:
        if isinstance(item, str):
            legacy = True
            refs.append({"key": item, "name": item, "rel_path": item + ".csv"})
        elif isinstance(item, dict):
            key = str(item.get("key", "")).strip()
            if not key:
                continue
            refs.append(
                {
                    "key": key,
                    "name": str(item.get("name") or key),
                    "rel_path": str(item.get("rel_path") or key + ".csv"),
                }
            )
    if legacy:
        for ref in refs:
            ref.setdefault(
                "note",
                "旧版快照仅记录了文件名，缺少子目录路径信息",
            )
    return refs


def _decode_snapshot(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["datasets"] = normalize_dataset_refs(json.loads(item.pop("datasets_json")))
    item["impl_ids"] = json.loads(item.pop("impl_ids_json"))
    item["summary"] = json.loads(item.pop("summary_json"))
    item["result"] = json.loads(item.pop("result_json"))
    return item


def create_run(conn: sqlite3.Connection, metrics: list[str], datasets_dir: str | Path,
               tolerances: dict[str, float],
               dataset_manifest: list[dict[str, Any]] | None = None) -> int:
    cursor = conn.execute(
        "INSERT INTO runs(status, started_at, metrics_json, datasets_dir, "
        "tolerances_json, datasets_manifest_json) "
        "VALUES ('running', ?, ?, ?, ?, ?)",
        (utc_now(), json.dumps(metrics, ensure_ascii=False), str(datasets_dir),
         json.dumps(tolerances, ensure_ascii=False),
         json.dumps(dataset_manifest or [], ensure_ascii=False)),
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
                  comparison: ComparisonResult,
                  evaluations: dict[str, ImplEvaluation],
                  dataset_refs: list[dict[str, Any]],
                  skipped: list[dict[str, str]] | None = None) -> int:
    result = comparison.to_dict()
    result["metric"] = metric.to_dict()
    result["implementations"] = [item.to_dict() for item in evaluations.values()]
    if skipped:
        result["unevaluable_impls"] = list(skipped)
    cursor = conn.execute(
        """
        INSERT INTO snapshots(run_id, metric_key, metric_version, datasets_json,
                              impl_ids_json, verdict, summary_json, result_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id, metric.key, comparison.metric_version,
            json.dumps(dataset_refs, ensure_ascii=False),
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
    raw_manifest = item.pop("datasets_manifest_json", None)
    item["datasets_manifest"] = (
        normalize_dataset_refs(json.loads(raw_manifest)) if raw_manifest else []
    )
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
    # Frozen at run time: every report/export of this run reads this exact
    # dataset set and ordering, never a fresh directory scan.
    manifest = catalog.manifest()
    run_id = create_run(conn, keys, datasets_dir, tolerances, manifest)
    log_audit(conn, "run.started", f"run:{run_id}", "ok", {"metrics": keys})
    summary = {verdict: 0 for verdict in VERDICTS}
    try:
        for key in keys:
            latest = get_latest(conn, key)
            metric = MetricDefinition.from_dict(latest["definition"])
            comparison, evaluations, skipped = run_metric(
                metric,
                int(latest["version"]),
                catalog,
                sql_roots,
                timeout_seconds=timeout_seconds,
                max_rows=max_rows,
                abs_tolerance=abs_tolerance,
                rel_tolerance=rel_tolerance,
            )
            refs = catalog.references_for(
                [item.source for item in metric.impls]
            )
            save_snapshot(
                conn, run_id, metric, comparison, evaluations, refs, skipped
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
