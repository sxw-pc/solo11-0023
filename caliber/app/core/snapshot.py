"""Comparison runs, immutable snapshots and drift detection."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from collections.abc import Mapping
from typing import Any

from ..audit import log_audit, record_error
from ..errors import CaliberError, NotFoundError, RunError
from ..models import (
    ComparisonResult,
    ImplEvaluation,
    MetricDefinition,
    MetricRunOutcome,
    utc_now,
)
from .evaluator import run_metric_isolated
from .versioning import get_latest, list_metrics

# Verdicts produced by an actual comparison.  ``degraded`` is a reserved
# non-comparison verdict emitted when an implementation timed out or hit the
# row cap: it is persisted for visibility but must never be aggregated as a
# consistency conclusion (no comparison was computed).
VERDICTS = ("consistent", "inconsistent", "partial")
DEGRADED_VERDICT = "degraded"
DEGRADED_REASONS = ("timeout", "row_limit")

# Execution metadata persisted in runs.summary_json.  The snapshots table
# keeps its original columns; the degradation markers live inside the
# existing summary_json / result_json payloads so old reports keep working.
DEGRADATION_POLICY = {
    "policy": "persist_degraded_snapshot",
    "snapshot_persisted": True,
    "reason": (
        "降级快照仍落库，使报告与看板能展示该指标被尝试过；但降级行不是比对结论，"
        "下游汇总必须先按标记过滤，避免把超时/截断值当成有效一致或不一致结果。"
    ),
    "downstream_detection": {
        "snapshot_verdict": DEGRADED_VERDICT,
        "summary_flag": "summary.degraded == true",
        "result_path": "result.degraded",
        "reason_field": "degraded_reason",
        "allowed_reasons": list(DEGRADED_REASONS),
    },
}


def is_degraded_snapshot(snapshot: Mapping[str, Any]) -> bool:
    """True when a snapshot carries no comparison conclusion.

    Downstream aggregation must use this (or ``summary.degraded`` /
    ``verdict == "degraded"``) to exclude degradation rows before treating
    values as a consistency verdict.
    """
    if snapshot.get("verdict") == DEGRADED_VERDICT:
        return True
    summary = snapshot.get("summary")
    return isinstance(summary, Mapping) and bool(summary.get("degraded"))


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
    return _insert_snapshot(
        conn,
        run_id,
        metric.key,
        comparison.metric_version,
        datasets,
        comparison.impl_ids,
        comparison.verdict,
        comparison.summary,
        result,
    )


def save_metric_outcome(
    conn: sqlite3.Connection, run_id: int, outcome: MetricRunOutcome
) -> int:
    """Persist either a normal comparison or a marked degraded snapshot."""
    payload = outcome.snapshot_payload()
    evaluations = outcome.evaluations
    datasets = sorted({item.source for item in evaluations.values()})
    impl_ids = (
        outcome.comparison.impl_ids
        if outcome.comparison is not None
        else [item.id for item in outcome.metric.impls]
    )
    return _insert_snapshot(
        conn,
        run_id,
        outcome.metric.key,
        outcome.version,
        datasets,
        impl_ids,
        str(payload["verdict"]),
        payload["summary"],
        payload["result"],
    )


def _insert_snapshot(
    conn: sqlite3.Connection,
    run_id: int,
    metric_key: str,
    metric_version: int,
    datasets: list[str],
    impl_ids: list[str],
    verdict: str,
    summary: dict[str, Any],
    result: dict[str, Any],
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO snapshots(run_id, metric_key, metric_version, datasets_json,
                              impl_ids_json, verdict, summary_json, result_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id, metric_key, int(metric_version),
            json.dumps(datasets, ensure_ascii=False),
            json.dumps(impl_ids, ensure_ascii=False),
            verdict,
            json.dumps(summary, ensure_ascii=False),
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
    # Degraded snapshots carry no comparison conclusion, so they can neither
    # produce nor break a consistency drift event.
    rows = conn.execute(
        "SELECT id, verdict, metric_version, created_at FROM snapshots "
        "WHERE metric_key = ? AND verdict IN ('consistent', 'inconsistent') "
        "ORDER BY id",
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
    """Run a batch of metrics, isolating timeout/row-cap degradation.

    Limits apply independently to every implementation of every metric.
    A metric that times out or exceeds the row cap is persisted as a marked
    ``degraded`` snapshot and the batch continues with the next metric.

    The overall run is ``completed`` when no hard failure occurred, even if
    some metrics degraded; ``summary_json`` separates normal verdict counts
    from ``degraded_timeout`` / ``degraded_row_limit`` counts.  Only a truly
    unexpected error aborts the run, leaving the already-saved snapshots in
    place and marking the run ``failed``.

    Every metric is evaluated with its own deadline computed inside
    ``evaluate_sql`` from ``timeout_seconds``; nothing is shared across the
    batch.
    """
    keys = list(dict.fromkeys(metric_keys or [item["key"] for item in list_metrics(conn)]))
    if not keys:
        raise RunError("没有可执行的指标，请先导入指标定义")
    tolerances = {"abs": float(abs_tolerance), "rel": float(rel_tolerance)}
    timeout_value = max(0.001, float(timeout_seconds))
    row_cap = max(1, int(max_rows))
    run_id = create_run(conn, keys, datasets_dir, tolerances)
    log_audit(conn, "run.started", f"run:{run_id}", "ok", {"metrics": keys})
    summary: dict[str, Any] = {
        **{verdict: 0 for verdict in VERDICTS},
        "metrics": len(keys),
        "completed": 0,
        "degraded": 0,
        "degraded_timeout": 0,
        "degraded_row_limit": 0,
        "failed": 0,
    }
    execution_meta: dict[str, Any] = {
        "limits": {
            "timeout_seconds": timeout_value,
            "max_rows": row_cap,
            "scope": "per_impl",
        },
        "degradation": dict(DEGRADATION_POLICY),
    }
    degraded_metrics: list[dict[str, Any]] = []
    try:
        for key in keys:
            latest = get_latest(conn, key)
            metric = MetricDefinition.from_dict(latest["definition"])
            outcome = run_metric_isolated(
                metric,
                int(latest["version"]),
                catalog,
                sql_roots,
                timeout_seconds=timeout_value,
                max_rows=row_cap,
                abs_tolerance=abs_tolerance,
                rel_tolerance=rel_tolerance,
            )
            save_metric_outcome(conn, run_id, outcome)
            if outcome.degraded:
                summary["degraded"] += 1
                if outcome.degraded_reason == "row_limit":
                    summary["degraded_row_limit"] += 1
                else:
                    summary["degraded_timeout"] += 1
                degraded_metrics.append(
                    {
                        "key": metric.key,
                        "version": int(latest["version"]),
                        "reason": outcome.degraded_reason,
                        "failed_impl_id": outcome.failed_impl_id,
                        "message": outcome.message,
                        "max_rows": outcome.max_rows,
                        "observed_rows": outcome.observed_rows,
                        "row_count_exact": outcome.row_count_exact,
                        "timeout_seconds": outcome.timeout_seconds,
                    }
                )
            else:
                summary["completed"] += 1
                verdict = outcome.comparison.verdict
                summary[verdict] = summary.get(verdict, 0) + 1
        summary["run_id"] = run_id
        summary["degraded_metrics"] = degraded_metrics
        summary["execution"] = execution_meta
        # A run with only degradation is still a successful run: every metric
        # was attempted and accounted for, and no hard failure occurred.
        finish_run(conn, run_id, status="completed", summary=summary)
        audit_status = "degraded" if summary["degraded"] else "ok"
        log_audit(
            conn,
            "run.completed",
            f"run:{run_id}",
            audit_status,
            {
                "metrics": summary["metrics"],
                "completed": summary["completed"],
                "degraded": summary["degraded"],
                "degraded_timeout": summary["degraded_timeout"],
                "degraded_row_limit": summary["degraded_row_limit"],
                "degraded_metrics": degraded_metrics,
            },
        )
        return run_id
    except Exception as exc:
        # Snapshots saved before the failure stay in place; the run is marked
        # failed so callers can tell a partial batch from a clean one.
        summary["failed"] = len(keys) - summary["completed"] - summary["degraded"]
        summary["degraded_metrics"] = degraded_metrics
        summary["execution"] = execution_meta
        finish_run(conn, run_id, status="failed", error=str(exc), summary=summary)
        record_error(conn, "run.failed", f"run:{run_id}", exc, extra={"metrics": keys})
        if isinstance(exc, CaliberError):
            raise
        raise RunError(f"比对运行失败: {exc}") from exc
