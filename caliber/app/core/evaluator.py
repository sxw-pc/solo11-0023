"""Read-only SQL evaluation for metric implementations."""
from __future__ import annotations

import re
import sqlite3
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

from ..errors import (
    CaliberError,
    EvaluationError,
    EvaluationTimeout,
    UnsafeSQLError,
)
from ..models import ComparisonResult, ImplEvaluation, ImplFailure, MetricDefinition
from .arbitration import (
    DEFAULT_STRATEGY,
    arbitrate,
    validate_strategy,
    validate_threshold,
)
from .comparator import compare_impl_results
from .dsl_validator import resolve_sql_path

FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|REPLACE|CREATE|DROP|ALTER|ATTACH|DETACH|PRAGMA|"
    r"VACUUM|REINDEX|ANALYZE|TRIGGER|BEGIN|COMMIT|ROLLBACK|SAVEPOINT|RELEASE|"
    r"LOAD_EXTENSION)\b",
    re.IGNORECASE,
)
START = re.compile(r"^\s*(SELECT|WITH)\b", re.IGNORECASE)


def _sanitized(sql: str) -> str:
    """Remove comments and quoted literals before keyword checks."""
    out: list[str] = []
    index, length = 0, len(sql)
    while index < length:
        char, nxt = sql[index], sql[index + 1: index + 2]
        if char == "-" and nxt == "-":
            newline = sql.find("\n", index + 2)
            index = length if newline < 0 else newline + 1
        elif char == "/" and nxt == "*":
            end = sql.find("*/", index + 2)
            if end < 0:
                raise UnsafeSQLError("SQL 块注释未闭合")
            index = end + 2
        elif char in {"'", '"', "`"}:
            quote = char
            index += 1
            while index < length:
                if sql[index] == quote:
                    if sql[index + 1: index + 2] == quote:
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
            else:
                raise UnsafeSQLError("SQL 字符串或标识符引号未闭合")
            out.append(" '' ")
        else:
            out.append(char)
            index += 1
    return "".join(out)


def validate_readonly_sql(sql: str) -> str:
    """Return normalized SQL or raise when it is not a single SELECT query."""
    if not isinstance(sql, str) or not sql.strip():
        raise UnsafeSQLError("SQL 不能为空")
    code = _sanitized(sql).strip()
    if code.endswith(";"):
        code = code[:-1].rstrip()
    if ";" in code:
        raise UnsafeSQLError("不允许一次执行多条 SQL 语句")
    if not START.match(code):
        raise UnsafeSQLError("只允许 SELECT 或 WITH 查询")
    match = FORBIDDEN.search(code)
    if match:
        raise UnsafeSQLError(f"禁止的 SQL 关键字: {match.group(1).upper()}")
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
    validated = validate_readonly_sql(sql)
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
        cursor = conn.execute(validated)
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


def evaluate_metric_impls(
    metric: MetricDefinition,
    version: int,
    catalog: Any,
    sql_roots: tuple[Path, ...] | list[Path],
    *,
    timeout_seconds: float = 5.0,
    max_rows: int = 10_000,
    collect_failures: bool = False,
    failures: list[ImplFailure] | None = None,
) -> dict[str, ImplEvaluation]:
    """Evaluate every implementation of a metric.

    By default the first failure aborts with an exception (historical
    behaviour).  With ``collect_failures=True`` each failed implementation is
    recorded in ``failures`` and evaluation continues with the remaining
    implementations, so one broken implementation cannot abort a run.
    """
    results: dict[str, ImplEvaluation] = {}
    conn = catalog.readonly_connection()
    for impl in metric.impls:
        try:
            catalog.require(impl.source)
            sql_path = resolve_sql_path(impl.sql_file, sql_roots)
            try:
                sql = sql_path.read_text(encoding="utf-8-sig")
            except OSError as exc:
                raise EvaluationError(
                    f"无法读取 SQL 文件: {sql_path}: {exc}"
                ) from exc
            values, row_count, duration = evaluate_sql(
                conn,
                sql,
                timeout_seconds=timeout_seconds,
                max_rows=max_rows,
                grain=metric.grain,
                impl_id=impl.id,
            )
        except Exception as exc:
            if not collect_failures:
                raise
            failure = _impl_failure(impl, exc)
            if failures is not None:
                failures.append(failure)
            continue
        results[impl.id] = ImplEvaluation(
            impl_id=impl.id,
            source=impl.source,
            sql_file=impl.sql_file,
            values=values,
            row_count=row_count,
            duration_ms=round(duration, 3),
        )
    return results


def _impl_failure(impl: Any, exc: Exception) -> ImplFailure:
    if isinstance(exc, CaliberError):
        code = exc.code
        reason = exc.message
    else:
        code = "internal_error"
        reason = f"{type(exc).__name__}: {exc}"
    if isinstance(exc, EvaluationTimeout):
        stage = "timeout"
    elif isinstance(exc, EvaluationError):
        stage = "evaluation"
    elif isinstance(exc, CaliberError):
        stage = "precondition"
    else:
        stage = "internal"
    return ImplFailure(
        impl_id=impl.id,
        source=impl.source,
        sql_file=impl.sql_file,
        stage=stage,
        code=code,
        reason=reason,
    )


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
    consensus_threshold: float | None = None,
    arbitration_strategy: str | None = None,
) -> tuple[ComparisonResult, dict[str, ImplEvaluation]]:
    threshold = validate_threshold(consensus_threshold)
    strategy = (
        validate_strategy(arbitration_strategy)
        if threshold is not None
        else DEFAULT_STRATEGY
    )
    failures: list[ImplFailure] = []
    evaluations = evaluate_metric_impls(
        metric,
        version,
        catalog,
        sql_roots,
        timeout_seconds=timeout_seconds,
        max_rows=max_rows,
        collect_failures=threshold is not None,
        failures=failures,
    )
    comparison = compare_impl_results(
        {impl_id: item.values for impl_id, item in evaluations.items()},
        metric_key=metric.key,
        metric_version=version,
        abs_tolerance=abs_tolerance,
        rel_tolerance=rel_tolerance,
    )
    if failures:
        comparison.impl_failures = list(failures)
        comparison.summary["failed_impl_count"] = len(failures)
        comparison.summary["failed_impl_ids"] = [item.impl_id for item in failures]
        if comparison.verdict == "consistent":
            # A failed implementation was never compared; calling the metric
            # fully consistent would hide that fact.
            comparison.verdict = "partial"
            comparison.summary["degraded_verdict"] = "存在未成功求值的实现"
    if threshold is not None:
        impl_order = [impl.id for impl in metric.impls]
        try:
            comparison.arbitration = arbitrate(
                {impl_id: item.values for impl_id, item in evaluations.items()},
                failures,
                abs_tolerance=abs_tolerance,
                rel_tolerance=rel_tolerance,
                threshold=threshold,
                strategy=strategy,
                impl_order=impl_order,
            )
        except Exception as exc:  # never let arbitration abort the metric
            comparison.arbitration = {
                "enabled": True,
                "status": "degraded",
                "strategy": strategy,
                "threshold": threshold,
                "degraded": {
                    "code": "arbitration_error",
                    "reason": "仲裁计算异常，已保留原有维度比对结果",
                    "detail": f"{type(exc).__name__}: {exc}",
                },
                "consensus_group": [],
                "dissenters": [],
                "dissent_dimensions": [],
                "minority": [],
                "max_divergence": 0.0,
                "max_divergence_detail": None,
                "pairwise": [],
                "non_arbitrable": [item.to_dict() for item in failures],
                "impl_classification": {},
                "summary": {
                    "impl_count": len(impl_order),
                    "arbitrable_impl_count": len(evaluations),
                    "consensus_count": 0,
                    "dissenter_count": 0,
                    "non_arbitrable_count": len(failures),
                    "dissent_dimension_count": 0,
                },
            }
        comparison.summary["arbitration_status"] = comparison.arbitration["status"]
    return comparison, evaluations
