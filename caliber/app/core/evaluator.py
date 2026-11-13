"""Read-only SQL evaluation for metric implementations."""
from __future__ import annotations

import re
import sqlite3
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

from ..errors import (
    EvaluationError,
    EvaluationRowLimit,
    EvaluationTimeout,
    UnsafeSQLError,
)
from ..models import ComparisonResult, ImplEvaluation, MetricDefinition, MetricRunOutcome
from .comparator import compare_impl_results
from .dsl_validator import resolve_sql_path

DEGRADED_TIMEOUT = "timeout"
DEGRADED_ROW_LIMIT = "row_limit"
ROW_COUNT_CHUNK = 1024

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


class QueryDeadline:
    """Monotonic deadline shared by one implementation evaluation.

    ``check`` is installed as an SQLite progress handler.  Returning 1 makes
    SQLite raise ``OperationalError: interrupted`` at the next VM step, which
    aborts the running statement (including inside a long cursor fetch) rather
    than merely refusing to start new ones.
    """

    def __init__(self, timeout_seconds: float) -> None:
        self.timeout_seconds = max(0.001, float(timeout_seconds))
        self.started = time.monotonic()
        self.deadline = self.started + self.timeout_seconds

    @property
    def elapsed_ms(self) -> float:
        return (time.monotonic() - self.started) * 1000

    def expired(self) -> bool:
        return time.monotonic() > self.deadline

    def check(self) -> int:
        return 1 if self.expired() else 0

    def install(self, conn: sqlite3.Connection) -> None:
        conn.set_progress_handler(self.check, 1000)

    def timeout_error(self, impl_id: str) -> EvaluationTimeout:
        return EvaluationTimeout(
            f"实现 {impl_id} 查询超时（{self.timeout_seconds}s）",
            details={
                "impl_id": impl_id,
                "timeout_seconds": self.timeout_seconds,
                "elapsed_ms": round(self.elapsed_ms, 3),
            },
        )


def _discard_cursor(cursor: sqlite3.Cursor | None) -> None:
    """Explicitly release an open cursor so it cannot pin a stale snapshot."""
    if cursor is not None:
        try:
            cursor.close()
        except sqlite3.Error:
            pass


def _safe_rollback(conn: sqlite3.Connection) -> None:
    try:
        if conn.in_transaction:
            conn.rollback()
    except sqlite3.Error:
        pass


def shutdown_readonly_connection(conn: sqlite3.Connection) -> None:
    """Return a shared query-only connection to a clean, idle state.

    Called after every metric so one degraded implementation cannot leave a
    pending transaction or progress handler that slows down later metrics.
    """
    try:
        conn.set_progress_handler(None, 0)
    except sqlite3.Error:
        pass
    _safe_rollback(conn)


def count_remaining_rows(
    cursor: sqlite3.Cursor,
    *,
    deadline: QueryDeadline | None = None,
    chunk_size: int = ROW_COUNT_CHUNK,
) -> tuple[int, bool]:
    """Drain a cursor and count its rows, respecting the optional deadline.

    Returns ``(count, exact)``.  ``exact`` is false when the query was
    interrupted (deadline expiry or an explicit cancel) during the drain;
    ``count`` is then the number of rows observed so far.
    """
    count = 0
    while True:
        if deadline is not None and deadline.expired():
            return count, False
        try:
            rows = cursor.fetchmany(chunk_size)
        except sqlite3.OperationalError as exc:
            if "interrupt" in str(exc).lower():
                return count, False
            raise
        if not rows:
            return count, True
        count += len(rows)


def evaluate_sql(
    conn: sqlite3.Connection,
    sql: str,
    *,
    timeout_seconds: float = 5.0,
    max_rows: int = 10_000,
    grain: str = "day",
    impl_id: str = "impl",
) -> tuple[dict[str, float], int, float]:
    """Execute one query on a query-only connection with timeout and row cap.

    The deadline interrupts the query in flight.  On a row-limit hit the
    cursor is drained to obtain the true pre-truncation row count (bounded by
    the same deadline); ``EvaluationRowLimit`` carries both the observed
    count and the cap.  Either way the connection is rolled back/closed and
    left usable for the next implementation.
    """
    validated = validate_readonly_sql(sql)
    deadline = QueryDeadline(timeout_seconds)
    _safe_rollback(conn)
    cursor: sqlite3.Cursor | None = None
    try:
        deadline.install(conn)
        conn.execute("BEGIN")
        cursor = conn.execute(validated)
        description = cursor.description or []
        if not 1 <= len(description) <= 2:
            raise EvaluationError("查询必须返回 1 列（数值）或 2 列（维度, 数值）")
        fetched = cursor.fetchmany(max_rows + 1)
        if len(fetched) > max_rows:
            observed_rows = len(fetched)
            remaining, exact = count_remaining_rows(cursor, deadline=deadline)
            raise EvaluationRowLimit(
                f"实现 {impl_id} 结果行数超过上限 {max_rows}",
                impl_id=impl_id,
                max_rows=max_rows,
                observed_rows=observed_rows + remaining,
                row_count_exact=exact,
                elapsed_ms=deadline.elapsed_ms,
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
        return values, len(fetched), deadline.elapsed_ms
    except sqlite3.OperationalError as exc:
        if "interrupt" in str(exc).lower():
            raise deadline.timeout_error(impl_id) from exc
        raise EvaluationError(f"实现 {impl_id} SQL 执行失败: {exc}") from exc
    except sqlite3.DatabaseError as exc:
        raise EvaluationError(f"实现 {impl_id} 数据库错误: {exc}") from exc
    finally:
        _discard_cursor(cursor)
        try:
            conn.set_progress_handler(None, 0)
        except sqlite3.Error:
            pass
        _safe_rollback(conn)


def _read_impl_sql(
    impl: Any, catalog: Any, sql_roots: tuple[Path, ...] | list[Path]
) -> str:
    catalog.require(impl.source)
    sql_path = resolve_sql_path(impl.sql_file, sql_roots)
    try:
        return sql_path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise EvaluationError(f"无法读取 SQL 文件: {sql_path}: {exc}") from exc


def evaluate_metric_impls(
    metric: MetricDefinition,
    version: int,
    catalog: Any,
    sql_roots: tuple[Path, ...] | list[Path],
    *,
    timeout_seconds: float = 5.0,
    max_rows: int = 10_000,
) -> dict[str, ImplEvaluation]:
    results: dict[str, ImplEvaluation] = {}
    conn = catalog.readonly_connection()
    try:
        for impl in metric.impls:
            sql = _read_impl_sql(impl, catalog, sql_roots)
            values, row_count, duration = evaluate_sql(
                conn,
                sql,
                timeout_seconds=timeout_seconds,
                max_rows=max_rows,
                grain=metric.grain,
                impl_id=impl.id,
            )
            results[impl.id] = ImplEvaluation(
                impl_id=impl.id,
                source=impl.source,
                sql_file=impl.sql_file,
                values=values,
                row_count=row_count,
                duration_ms=round(duration, 3),
            )
    finally:
        # One shared connection is reused by every implementation; always
        # leave it clean, even when an implementation degraded.
        shutdown_readonly_connection(conn)
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
) -> tuple[ComparisonResult, dict[str, ImplEvaluation]]:
    evaluations = evaluate_metric_impls(
        metric,
        version,
        catalog,
        sql_roots,
        timeout_seconds=timeout_seconds,
        max_rows=max_rows,
    )
    comparison = compare_impl_results(
        {impl_id: item.values for impl_id, item in evaluations.items()},
        metric_key=metric.key,
        metric_version=version,
        abs_tolerance=abs_tolerance,
        rel_tolerance=rel_tolerance,
    )
    return comparison, evaluations


def _degraded_evaluation(impl: Any, *, error: EvaluationError) -> ImplEvaluation:
    details = error.details if isinstance(error.details, dict) else {}
    return ImplEvaluation(
        impl_id=impl.id,
        source=impl.source,
        sql_file=impl.sql_file,
        values={},
        row_count=int(details.get("observed_rows", 0) or 0),
        duration_ms=float(details.get("elapsed_ms", 0.0) or 0.0),
        status="degraded",
        error_code=error.code,
        message=error.message,
        row_count_exact=bool(details.get("row_count_exact", False)),
        max_rows=details.get("max_rows"),
    )


def run_metric_isolated(
    metric: MetricDefinition,
    version: int,
    catalog: Any,
    sql_roots: tuple[Path, ...] | list[Path],
    *,
    timeout_seconds: float = 5.0,
    max_rows: int = 10_000,
    abs_tolerance: float = 1e-6,
    rel_tolerance: float = 0.0,
) -> MetricRunOutcome:
    """Evaluate one metric without letting degradation abort the batch.

    Timeout and row-limit failures are converted into a degraded outcome.
    Any other error (unsafe SQL, missing dataset, non-numeric value, ...) is a
    hard failure and is still raised to the caller.

    A degraded metric stops at the first failing implementation: running the
    remaining implementations would only burn additional time on a metric
    whose comparison can no longer be trusted.
    """
    evaluations: dict[str, ImplEvaluation] = {}
    conn = catalog.readonly_connection()
    try:
        for impl in metric.impls:
            sql = _read_impl_sql(impl, catalog, sql_roots)
            try:
                values, row_count, duration = evaluate_sql(
                    conn,
                    sql,
                    timeout_seconds=timeout_seconds,
                    max_rows=max_rows,
                    grain=metric.grain,
                    impl_id=impl.id,
                )
            except EvaluationRowLimit as exc:
                evaluations[impl.id] = _degraded_evaluation(impl, error=exc)
                return MetricRunOutcome(
                    metric=metric,
                    version=version,
                    status="degraded",
                    comparison=None,
                    evaluations=evaluations,
                    degraded_reason=DEGRADED_ROW_LIMIT,
                    error_code=exc.code,
                    message=exc.message,
                    failed_impl_id=impl.id,
                    max_rows=exc.max_rows,
                    observed_rows=exc.observed_rows,
                    row_count_exact=exc.row_count_exact,
                )
            except EvaluationTimeout as exc:
                evaluations[impl.id] = _degraded_evaluation(impl, error=exc)
                return MetricRunOutcome(
                    metric=metric,
                    version=version,
                    status="degraded",
                    comparison=None,
                    evaluations=evaluations,
                    degraded_reason=DEGRADED_TIMEOUT,
                    error_code=exc.code,
                    message=exc.message,
                    failed_impl_id=impl.id,
                    timeout_seconds=float(
                        (exc.details or {}).get("timeout_seconds", timeout_seconds)
                    ),
                )
            evaluations[impl.id] = ImplEvaluation(
                impl_id=impl.id,
                source=impl.source,
                sql_file=impl.sql_file,
                values=values,
                row_count=row_count,
                duration_ms=round(duration, 3),
            )
    finally:
        shutdown_readonly_connection(conn)
    comparison = compare_impl_results(
        {impl_id: item.values for impl_id, item in evaluations.items()},
        metric_key=metric.key,
        metric_version=version,
        abs_tolerance=abs_tolerance,
        rel_tolerance=rel_tolerance,
    )
    return MetricRunOutcome(
        metric=metric,
        version=version,
        status="ok",
        comparison=comparison,
        evaluations=evaluations,
    )
