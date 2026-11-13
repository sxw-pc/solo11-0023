"""Read-only SQL evaluation for metric implementations."""
from __future__ import annotations

import sqlite3
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

from ..audit import log_audit
from ..errors import EvaluationError, EvaluationTimeout, UnsafeSQLError
from ..models import ComparisonResult, ImplEvaluation, MetricDefinition
from .comparator import compare_impl_results
from .dsl_validator import resolve_sql_path
from .sql_gate import GATE_META, AccessScope, SqlGate


def validate_readonly_sql(sql: str) -> str:
    """兼容入口：无连接降级模式下的只读校验，拒绝时抛 UnsafeSQLError。

    完整判定（EXPLAIN 字节码 + 表级越权）见 :class:`sql_gate.SqlGate`，
    导入与运行统一走该模块；本函数只保留给直接单元调用。
    """
    if not isinstance(sql, str) or not sql.strip():
        raise UnsafeSQLError("SQL 不能为空")
    decision = SqlGate(None).inspect_text(sql)
    if not decision.allowed:
        raise UnsafeSQLError(
            decision.primary_violation.message,
            details={"gate": decision.to_dict()},
        )
    return sql.strip()


def _parse_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for parser in (date.fromisoformat, lambda item: datetime.fromisoformat(item).date()):
        try:
            return parser(text)
        except (ValueError, TypeError):
            continue
    return None


def truncate_dimension(value: Any, grain: str) -> str:
    if value is None:
        return "__null__"
    text = str(value).strip()
    if not text:
        return "__null__"
    parsed = _parse_date(text)
    if parsed is None:
        return text
    if grain == "day":
        return parsed.isoformat()
    if grain == "week":
        iso = parsed.isocalendar()
        return f"{iso.year:04d}-W{iso.week:02d}"
    if grain == "month":
        return f"{parsed.year:04d}-{parsed.month:02d}"
    return text


def _numeric(value: Any, impl_id: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EvaluationError(
            f"实现 {impl_id} 返回的指标值不是数值: {value!r}",
            details={"impl_id": impl_id},
        )
    return float(value)


def evaluate_sql(
    conn: sqlite3.Connection,
    sql: str,
    *,
    timeout_seconds: float = 5.0,
    max_rows: int = 10_000,
    grain: str = "day",
    impl_id: str = "impl",
) -> tuple[dict[str, float], int, float]:
    """Execute one query on a query-only connection with timeout and row cap."""
    started = time.monotonic()
    deadline = started + max(0.001, float(timeout_seconds))
    conn.set_progress_handler(
        lambda: 1 if time.monotonic() > deadline else 0,
        1000,
    )
    if conn.in_transaction:
        conn.rollback()
    try:
        conn.execute("BEGIN")
        cursor = conn.execute(sql)
        description = cursor.description or []
        if not 1 <= len(description) <= 2:
            raise EvaluationError("查询必须返回 1 列（数值）或 2 列（维度, 数值）")
        fetched = cursor.fetchmany(max_rows + 1)
        if len(fetched) > max_rows:
            raise EvaluationError(
                f"结果行数超过上限 {max_rows}",
                code="row_limit_exceeded",
                details={"impl_id": impl_id, "max_rows": max_rows},
            )
        values: dict[str, float] = {}
        for row in fetched:
            if len(description) == 1:
                dimension, value = "__all__", row[0]
            else:
                dimension = truncate_dimension(row[0], grain)
                value = row[1]
            if dimension in values:
                raise EvaluationError(
                    f"实现 {impl_id} 返回重复维度: {dimension}",
                    details={"impl_id": impl_id, "dimension": dimension},
                )
            values[dimension] = _numeric(value, impl_id)
        conn.rollback()
        return values, len(fetched), (time.monotonic() - started) * 1000
    except sqlite3.OperationalError as exc:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        if "interrupt" in str(exc).lower():
            raise EvaluationTimeout(
                f"实现 {impl_id} 查询超时（{timeout_seconds}s）",
                details={"impl_id": impl_id, "timeout_seconds": timeout_seconds},
            ) from exc
        raise EvaluationError(f"实现 {impl_id} SQL 执行失败: {exc}") from exc
    except sqlite3.DatabaseError as exc:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        raise EvaluationError(f"实现 {impl_id} 数据库错误: {exc}") from exc
    finally:
        conn.set_progress_handler(None, 0)


def _rejected_evaluation(
    impl: Any,
    decision: Any,
    reason: dict[str, Any],
) -> ImplEvaluation:
    return ImplEvaluation(
        impl_id=impl.id,
        source=impl.source,
        sql_file=impl.sql_file,
        values={},
        row_count=0,
        duration_ms=0.0,
        rejected=True,
        rejection_reason=reason,
        gate_mode=decision.mode,
        sql_sha256=decision.sql_sha256,
        whitelisted=bool(decision.whitelisted),
    )


def evaluate_metric_impls(
    metric: MetricDefinition,
    version: int,
    catalog: Any,
    sql_roots: tuple[Path, ...] | list[Path],
    *,
    timeout_seconds: float = 5.0,
    max_rows: int = 10_000,
    gate: SqlGate | None = None,
    audit_conn: sqlite3.Connection | None = None,
) -> dict[str, ImplEvaluation]:
    """逐个实现执行；单个实现被门禁拒绝时只标记该实现，不影响其他实现。"""
    results: dict[str, ImplEvaluation] = {}
    conn = catalog.readonly_connection()
    gate = gate or SqlGate(conn)
    for impl in metric.impls:
        catalog.require(impl.source)
        sql_path = resolve_sql_path(impl.sql_file, sql_roots)
        try:
            sql = sql_path.read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise EvaluationError(f"无法读取 SQL 文件: {sql_path}: {exc}") from exc

        # 运行前再核一次：即使导入时通过，文件被替换也会因哈希变化重新校验。
        decision = gate.inspect_text(
            sql,
            access_scope=AccessScope.from_raw(impl.access),
            allowlist=impl.allowlist,
            context={
                "metric_key": metric.key,
                "impl_id": impl.id,
                "sql_file": impl.sql_file,
                "stage": "run",
            },
        )
        for passed in decision.whitelisted:
            if audit_conn is not None:
                log_audit(
                    audit_conn,
                    "sql_gate.whitelist",
                    f"{metric.key}/{impl.id}",
                    "ok",
                    {
                        "metric_key": metric.key,
                        "impl_id": impl.id,
                        "sql_file": impl.sql_file,
                        "entry": passed.to_dict(),
                        **decision.audit_summary(),
                    },
                )
        if not decision.allowed:
            reason = decision.rejection_reason() or {}
            results[impl.id] = _rejected_evaluation(impl, decision, reason)
            if audit_conn is not None:
                log_audit(
                    audit_conn,
                    "sql_gate.rejected",
                    f"{metric.key}/{impl.id}",
                    "rejected",
                    {
                        "metric_key": metric.key,
                        "impl_id": impl.id,
                        "sql_file": impl.sql_file,
                        "reason": reason,
                        "gate": decision.to_dict(),
                    },
                )
            continue

        try:
            values, row_count, duration = evaluate_sql(
                conn,
                sql,
                timeout_seconds=timeout_seconds,
                max_rows=max_rows,
                grain=metric.grain,
                impl_id=impl.id,
            )
        except EvaluationError as exc:
            # 执行期错误同样按单实现隔离，避免整批 run 中断。
            decision_context = decision.to_dict()
            reason = {
                "rule": exc.code,
                "message": exc.message,
                "statement_type": decision.statement_type,
                "gate_mode": decision.mode,
            }
            results[impl.id] = _rejected_evaluation(impl, decision, reason)
            if audit_conn is not None:
                log_audit(
                    audit_conn,
                    "sql_gate.evaluation_failed",
                    f"{metric.key}/{impl.id}",
                    "error",
                    {"metric_key": metric.key, "impl_id": impl.id, "error": str(exc),
                     "gate": decision_context},
                )
            continue
        results[impl.id] = ImplEvaluation(
            impl_id=impl.id,
            source=impl.source,
            sql_file=impl.sql_file,
            values=values,
            row_count=row_count,
            duration_ms=round(duration, 3),
            rejected=False,
            gate_mode=decision.mode,
            sql_sha256=decision.sql_sha256,
            whitelisted=bool(decision.whitelisted),
        )
    return results


def run_metric(
    metric: MetricDefinition,
    version: int,
    catalog: Any,
    sql_roots: tuple[Path, ...] | list[Path],
    *,
    timeout_seconds: float = 5.0,
    max_rows: int = 10_000,
    abs_tolerance: float = 1e-6,
    rel_tolerance: float = 0.0,
    gate: SqlGate | None = None,
    audit_conn: sqlite3.Connection | None = None,
) -> tuple[ComparisonResult, dict[str, ImplEvaluation]]:
    evaluations = evaluate_metric_impls(
        metric,
        version,
        catalog,
        sql_roots,
        timeout_seconds=timeout_seconds,
        max_rows=max_rows,
        gate=gate,
        audit_conn=audit_conn,
    )
    allowed = {
        impl_id: item.values
        for impl_id, item in evaluations.items()
        if not item.rejected
    }
    if not allowed:
        comparison = ComparisonResult(
            metric_key=metric.key,
            metric_version=version,
            impl_ids=list(evaluations),
            verdict="rejected",
            dimensions=[],
            diffs=[],
            summary={
                "impl_count": len(evaluations),
                "rejected_impls": [
                    item.rejection_reason or {}
                    for item in evaluations.values()
                    if item.rejected
                ],
                "gate_meta": GATE_META,
            },
            abs_tolerance=float(abs_tolerance),
            rel_tolerance=float(rel_tolerance),
        )
        return comparison, evaluations
    comparison = compare_impl_results(
        allowed,
        metric_key=metric.key,
        metric_version=version,
        abs_tolerance=abs_tolerance,
        rel_tolerance=rel_tolerance,
    )
    # 把被拒实现与门禁元信息补进快照摘要，保留可追溯的拒绝原因。
    rejected = [
        {"impl_id": item.impl_id, "reason": item.rejection_reason,
         "gate_mode": item.gate_mode, "sql_sha256": item.sql_sha256}
        for item in evaluations.values()
        if item.rejected
    ]
    if rejected:
        comparison.summary["rejected_impls"] = rejected
    comparison.summary["gate_meta"] = GATE_META
    return comparison, evaluations
