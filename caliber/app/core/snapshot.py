"""Comparison runs, immutable snapshots and drift detection."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..audit import log_audit, record_error
from ..errors import CaliberError, NotFoundError, RunError
from ..models import ComparisonResult, ImplEvaluation, MetricDefinition, utc_now
from .evaluator import run_metric
from .executor import (
    DEFAULT_MAX_WORKERS,
    STATUS_FAILED,
    STATUS_SKIPPED,
    STATUS_SUCCESS,
    ConcurrentRuleExecutor,
    RuleContext,
    WorkerScope,
)
from .versioning import get_latest, list_metrics

VERDICTS = ("consistent", "inconsistent", "partial")
# Batch-level run states.  ``completed`` means every rule succeeded;
# ``completed_with_errors`` means the batch itself finished but some rules
# failed independently — successful results were still retained.
RUN_COMPLETED = "completed"
RUN_COMPLETED_WITH_ERRORS = "completed_with_errors"
RUN_FAILED = "failed"


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


def _decode_run_item(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


def save_run_item(conn: sqlite3.Connection, run_id: int, item: dict[str, Any]) -> int:
    """Persist the terminal state of one rule within a run."""
    cursor = conn.execute(
        """
        INSERT INTO run_items(run_id, position, metric_key, metric_version, status,
                              verdict, snapshot_id, error, error_code,
                              duration_ms, worker)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            int(item["position"]),
            item["metric_key"],
            item.get("metric_version"),
            item["status"],
            item.get("verdict"),
            item.get("snapshot_id"),
            item.get("error"),
            item.get("error_code"),
            float(item.get("duration_ms") or 0.0),
            item.get("worker"),
        ),
    )
    conn.commit()
    return int(cursor.lastrowid or 0)


def run_items_for_run(conn: sqlite3.Connection, run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM run_items WHERE run_id = ? ORDER BY position", (run_id,)
    ).fetchall()
    return [_decode_run_item(row) for row in rows]


def _latest_resumable_run(
    conn: sqlite3.Connection, keys: list[str]
) -> dict[str, Any] | None:
    """Find the most recent run covering exactly this rule list, if any."""
    rows = conn.execute(
        "SELECT * FROM runs WHERE status != 'failed' ORDER BY id DESC"
    ).fetchall()
    wanted = list(keys)
    for row in rows:
        try:
            previous_keys = json.loads(row["metrics_json"])
        except (TypeError, json.JSONDecodeError):
            continue
        if previous_keys == wanted:
            return _decode_run(row)
    return None


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


@dataclass(slots=True)
class _RuleJob:
    key: str
    position: int
    metric: MetricDefinition | None = None
    version: int | None = None
    skip: bool = False
    verdict: str | None = None
    snapshot_id: int | None = None
    error: str | None = None
    error_code: str | None = None


class _CatalogWorkerScope(WorkerScope):
    """Worker-lifetime resources: one private read-only SQLite connection.

    The connection is checked out of the catalog's pre-provisioned pool when a
    worker starts and returned when the worker scope closes; the catalog owns
    the actual connection lifecycle and closes everything on shutdown.
    """

    def __init__(self, name: str, catalog: Any) -> None:
        super().__init__(name, catalog.acquire_worker_connection())
        self._catalog = catalog

    def close(self) -> None:
        if self.resource is not None:
            self._catalog.release_worker_connection(self.resource)
            self.resource = None

def _plan_jobs(
    conn: sqlite3.Connection,
    keys: list[str],
    prior_run: dict[str, Any] | None,
) -> list[_RuleJob]:
    """Resolve every key to its current definition and decide skip/run.

    Already-succeeded rules from the run being resumed are skipped here so a
    re-run only fills in failed and never-executed rules; their prior snapshot
    stays untouched and remains part of the result set.
    """
    succeeded: dict[str, dict[str, Any]] = {}
    if prior_run is not None:
        for item in run_items_for_run(conn, int(prior_run["id"])):
            if item["status"] == STATUS_SUCCESS and item["snapshot_id"] is not None:
                succeeded[item["metric_key"]] = item
    jobs: list[_RuleJob] = []
    missing: list[str] = []
    for position, key in enumerate(keys):
        prior = succeeded.get(key)
        if prior is not None:
            jobs.append(
                _RuleJob(
                    key=key,
                    position=position,
                    skip=True,
                    verdict=prior.get("verdict"),
                    snapshot_id=prior.get("snapshot_id"),
                )
            )
            continue
        try:
            latest = get_latest(conn, key)
        except NotFoundError:
            missing.append(key)
            jobs.append(_RuleJob(key=key, position=position))
            continue
        jobs.append(
            _RuleJob(
                key=key,
                position=position,
                metric=MetricDefinition.from_dict(latest["definition"]),
                version=int(latest["version"]),
            )
        )
    if missing and len(missing) == len([job for job in jobs if not job.skip]):
        # Every rule we would actually execute is unknown: keep the serial
        # behaviour of failing loudly before creating any evaluation work.
        raise NotFoundError(f"指标不存在: {missing[0]}")
    return jobs


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
    max_workers: int = DEFAULT_MAX_WORKERS,
    resume: bool = False,
) -> int:
    """Run all requested rules through a bounded concurrent executor.

    Concurrency, queuing and failure isolation cooperate here: the executor
    never runs more than ``max_workers`` rules at once, overflow rules queue on
    a bounded semaphore, and a rule-level exception is captured into that
    rule's own ``run_items`` row instead of aborting the batch.  Every worker
    owns a private read-only connection that is released on success, failure
    and cancellation.
    """
    keys = list(dict.fromkeys(metric_keys or [item["key"] for item in list_metrics(conn)]))
    if not keys:
        raise RunError("没有可执行的指标，请先导入指标定义")
    tolerances = {"abs": float(abs_tolerance), "rel": float(rel_tolerance)}

    prior_run = _latest_resumable_run(conn, keys) if resume else None
    jobs = _plan_jobs(conn, keys, prior_run)

    run_id = create_run(conn, keys, datasets_dir, tolerances)
    log_audit(
        conn,
        "run.started",
        f"run:{run_id}",
        "ok",
        {"metrics": keys, "max_workers": int(max_workers), "resume": bool(resume)},
    )

    verdict_counts = {verdict: 0 for verdict in VERDICTS}
    status_counts = {STATUS_SUCCESS: 0, STATUS_FAILED: 0, STATUS_SKIPPED: 0}

    # Record skipped rules up front: their successful results are inherited
    # from the prior run and must not be recomputed or lost.
    for job in jobs:
        if not job.skip:
            continue
        save_run_item(
            conn,
            run_id,
            {
                "position": job.position,
                "metric_key": job.key,
                "metric_version": None,
                "status": STATUS_SKIPPED,
                "verdict": job.verdict,
                "snapshot_id": job.snapshot_id,
            },
        )
        status_counts[STATUS_SKIPPED] += 1
        if job.verdict in verdict_counts:
            verdict_counts[job.verdict] += 1

    runnable = [job for job in jobs if not job.skip]
    pool_size = min(int(max_workers), max(1, len(runnable)))
    # Build the worker read-only connections before any worker starts; the
    # in-memory SQLite backup is serialized by the catalog lock.  Skip this
    # entirely when every rule was already satisfied by a resumed run.
    if runnable:
        catalog.provision_worker_connections(pool_size)
    executor = ConcurrentRuleExecutor(
        lambda name: _CatalogWorkerScope(name, catalog),
        max_workers=max_workers,
    )

    def _task(worker_conn: Any, context: RuleContext) -> tuple[ComparisonResult, dict[str, ImplEvaluation]]:
        job = runnable[context.index]
        if job.metric is None or job.version is None:
            # A missing definition for one rule inside an otherwise valid list
            # fails that rule alone instead of tearing down the whole batch.
            raise NotFoundError(f"指标不存在: {job.key}")
        return run_metric(
            job.metric,
            job.version,
            catalog,
            sql_roots,
            timeout_seconds=timeout_seconds,
            max_rows=max_rows,
            abs_tolerance=abs_tolerance,
            rel_tolerance=rel_tolerance,
            connection=worker_conn,
        )

    interrupted = False
    try:
        # Streaming consumption: each terminal outcome is persisted immediately,
        # so a later failure or cancellation keeps the successes already earned.
        for outcome in executor.run_rules_stream(
            [(job.key, job) for job in runnable], _task
        ):
            job = runnable[outcome.index]
            if outcome.status == STATUS_SUCCESS:
                comparison, evaluations = outcome.result
                snapshot_id = save_snapshot(
                    conn, run_id, job.metric, comparison, evaluations
                )
                save_run_item(
                    conn,
                    run_id,
                    {
                        "position": job.position,
                        "metric_key": job.key,
                        "metric_version": job.version,
                        "status": STATUS_SUCCESS,
                        "verdict": comparison.verdict,
                        "snapshot_id": snapshot_id,
                        "duration_ms": outcome.duration_ms,
                        "worker": outcome.worker,
                    },
                )
                status_counts[STATUS_SUCCESS] += 1
                verdict_counts[comparison.verdict] = (
                    verdict_counts.get(comparison.verdict, 0) + 1
                )
            else:
                # Failure isolation: mark this rule failed, keep going.  The
                # audit trail gets one error record per failed rule.
                failure = RunError(outcome.error or "规则执行失败")
                failure.code = outcome.error_code or RunError.code
                record_error(
                    conn,
                    "run.item_failed",
                    f"run:{run_id}:{job.key}",
                    failure,
                    extra={"metric": job.key, "position": job.position},
                )
                save_run_item(
                    conn,
                    run_id,
                    {
                        "position": job.position,
                        "metric_key": job.key,
                        "metric_version": job.version,
                        "status": STATUS_FAILED,
                        "error": outcome.error,
                        "error_code": outcome.error_code,
                        "duration_ms": outcome.duration_ms,
                        "worker": outcome.worker,
                    },
                )
                status_counts[STATUS_FAILED] += 1
    except KeyboardInterrupt:
        interrupted = True
    except Exception as exc:  # defensive: executor bugs must still finalize the run
        record_error(conn, "run.failed", f"run:{run_id}", exc, extra={"metrics": keys})
        finish_run(
            conn,
            run_id,
            status=RUN_FAILED,
            error=str(exc),
            summary=_run_summary(run_id, keys, status_counts, verdict_counts, executor),
        )
        if isinstance(exc, CaliberError):
            raise
        raise RunError(f"比对运行失败: {exc}") from exc

    summary = _run_summary(run_id, keys, status_counts, verdict_counts, executor)
    if interrupted:
        # Re-raise after durable state and resources have been settled; the
        # executor's finally has already reclaimed every worker connection.
        finish_run(conn, run_id, status=RUN_FAILED, error="运行被用户中断", summary=summary)
        log_audit(conn, "run.cancelled", f"run:{run_id}", "error", summary)
        raise KeyboardInterrupt
    if status_counts[STATUS_FAILED]:
        finish_run(conn, run_id, status=RUN_COMPLETED_WITH_ERRORS, summary=summary)
        log_audit(conn, "run.completed_with_errors", f"run:{run_id}", "error", summary)
    else:
        finish_run(conn, run_id, status=RUN_COMPLETED, summary=summary)
        log_audit(conn, "run.completed", f"run:{run_id}", "ok", summary)
    return run_id


def _run_summary(
    run_id: int,
    keys: list[str],
    status_counts: dict[str, int],
    verdict_counts: dict[str, int],
    executor: ConcurrentRuleExecutor,
) -> dict[str, Any]:
    summary: dict[str, Any] = {verdict: verdict_counts.get(verdict, 0) for verdict in VERDICTS}
    summary.update(
        {
            "metrics": len(keys),
            "run_id": run_id,
            "succeeded": status_counts[STATUS_SUCCESS],
            "failed": status_counts[STATUS_FAILED],
            "skipped": status_counts[STATUS_SKIPPED],
            "max_workers": executor.max_workers,
            "peak_workers": executor.peak_workers,
        }
    )
    return summary
