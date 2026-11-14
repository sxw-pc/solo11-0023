"""Read-only SQL evaluation for metric implementations."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

from ..errors import EvaluationError, EvaluationTimeout, UnsafeSQLError
from ..models import ComparisonResult, ImplEvaluation, MetricDefinition
from .attribution import (
    attribute_differences,
    degraded_from_error,
    is_numeric,
    skipped_attribution,
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


def _coerce_detail_value(value: Any) -> Any:
    """Best-effort numeric coercion for detail rows.

    SQLite may return TEXT for numeric-looking values (CSV-loaded tables);
    genuine strings stay strings so type-mismatch clusters remain detectable.
    """
    if isinstance(value, (int, float)) or value is None:
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return int(text)
        except ValueError:
            pass
        try:
            return float(text)
        except ValueError:
            return value
    return value


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
    values, row_count, duration, _detail = _execute_query(
        conn,
        sql,
        timeout_seconds=timeout_seconds,
        max_rows=max_rows,
        grain=grain,
        impl_id=impl_id,
        capture_detail=False,
        spill_dir=None,
    )
    return values, row_count, duration


def evaluate_sql_with_detail(
    conn: sqlite3.Connection,
    sql: str,
    *,
    spill_dir: str,
    timeout_seconds: float = 5.0,
    max_rows: int = 10_000,
    grain: str = "day",
    impl_id: str = "impl",
) -> tuple[dict[str, float], int, float, str]:
    """Like :func:`evaluate_sql` but also stream rows into a JSONL spill file.

    The spill file's first line is the column-name array; every following line
    is one raw row as a JSON object keyed by column name.  Multi-column
    queries (composite natural keys plus one value column) are allowed here.
    """
    values, row_count, duration, detail_file = _execute_query(
        conn,
        sql,
        timeout_seconds=timeout_seconds,
        max_rows=max_rows,
        grain=grain,
        impl_id=impl_id,
        capture_detail=True,
        spill_dir=spill_dir,
    )
    if detail_file is None:  # pragma: no cover - defensive
        raise EvaluationError(f"实现 {impl_id} 明细暂存失败")
    return values, row_count, duration, detail_file


def _dimension_from_row(columns: list[str], row: tuple[Any, ...], grain: str) -> str:
    if len(columns) == 1:
        return "__all__"
    if len(columns) == 2:
        return truncate_dimension(row[0], grain)
    # 多列结果：除最后一列（指标值）外全部视为维度，用 | 连接并按粒度截断
    parts = [truncate_dimension(value, grain) for value in row[:-1]]
    return "|".join(parts)


def _execute_query(
    conn: sqlite3.Connection,
    sql: str,
    *,
    timeout_seconds: float,
    max_rows: int,
    grain: str,
    impl_id: str,
    capture_detail: bool,
    spill_dir: str | None,
) -> tuple[dict[str, float], int, float, str | None]:
    validated = validate_readonly_sql(sql)
    started = time.monotonic()
    deadline = started + max(0.001, float(timeout_seconds))
    conn.set_progress_handler(
        lambda: 1 if time.monotonic() > deadline else 0,
        1000,
    )
    detail_handle = None
    detail_path: str | None = None
    if conn.in_transaction:
        conn.rollback()
    try:
        conn.execute("BEGIN")
        cursor = conn.execute(validated)
        description = cursor.description or []
        column_count = len(description)
        if capture_detail:
            if not 2 <= column_count <= 6:
                raise EvaluationError(
                    "归因查询必须返回至少一列自然键加一列指标值（2~6 列）"
                )
            fd, detail_path = tempfile.mkstemp(
                prefix=f"caliber-detail-{impl_id}-", suffix=".jsonl", dir=spill_dir
            )
            detail_handle = os.fdopen(fd, "w", encoding="utf-8")
            columns = [item[0] for item in description]
            detail_handle.write(
                json.dumps(columns, ensure_ascii=False) + "\n"
            )
        elif not 1 <= column_count <= 2:
            raise EvaluationError("查询必须返回 1 列（数值）或 2 列（维度, 数值）")
        columns = [item[0] for item in description]
        fetched = cursor.fetchmany(max_rows + 1)
        if len(fetched) > max_rows:
            raise EvaluationError(
                f"结果行数超过上限 {max_rows}",
                code="row_limit_exceeded",
                details={"impl_id": impl_id, "max_rows": max_rows},
            )
        values: dict[str, float] = {}
        for row in fetched:
            raw_value = row[-1]
            if capture_detail:
                # 明细模式允许 TEXT/NULL 指标值（用于类型不一致簇），尽量转数值
                value: Any = _coerce_detail_value(raw_value)
                dimension_value = float(value) if is_numeric(value) else None
                dimension = "|".join(
                    truncate_dimension(part, grain) for part in row[:-1]
                )
            else:
                value = raw_value
                dimension_value = _numeric(value, impl_id)
                dimension = _dimension_from_row(columns, row, grain)
            if dimension in values:
                if not capture_detail:
                    raise EvaluationError(
                        f"实现 {impl_id} 返回重复维度: {dimension}",
                        details={"impl_id": impl_id, "dimension": dimension},
                    )
            if dimension_value is not None:
                values.setdefault(dimension, dimension_value)
            if capture_detail and detail_handle is not None:
                detail_row = list(row[:-1]) + [value]
                detail_handle.write(
                    json.dumps(
                        dict(zip(columns, detail_row)),
                        ensure_ascii=False,
                        default=str,
                    )
                    + "\n"
                )
        conn.rollback()
        if detail_handle is not None:
            detail_handle.close()
            detail_handle = None
        return values, len(fetched), (time.monotonic() - started) * 1000, detail_path
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
        if detail_handle is not None:
            # 异常路径：句柄仍开着说明没有正常返回，关闭并删除半成品暂存文件
            detail_handle.close()
            if detail_path:
                try:
                    os.unlink(detail_path)
                except OSError:
                    pass


def evaluate_metric_impls(
    metric: MetricDefinition,
    version: int,
    catalog: Any,
    sql_roots: tuple[Path, ...] | list[Path],
    *,
    timeout_seconds: float = 5.0,
    max_rows: int = 10_000,
    capture_detail: bool = False,
    spill_dir: str | None = None,
) -> dict[str, ImplEvaluation]:
    results: dict[str, ImplEvaluation] = {}
    conn = catalog.readonly_connection()
    if capture_detail and spill_dir is None:
        raise EvaluationError("启用明细捕获时必须提供 spill_dir")
    for impl in metric.impls:
        catalog.require(impl.source)
        sql_path = resolve_sql_path(impl.sql_file, sql_roots)
        try:
            sql = sql_path.read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise EvaluationError(f"无法读取 SQL 文件: {sql_path}: {exc}") from exc
        if capture_detail:
            values, row_count, duration, detail_file = evaluate_sql_with_detail(
                conn,
                sql,
                spill_dir=spill_dir or "",
                timeout_seconds=timeout_seconds,
                max_rows=max_rows,
                grain=metric.grain,
                impl_id=impl.id,
            )
        else:
            values, row_count, duration = evaluate_sql(
                conn,
                sql,
                timeout_seconds=timeout_seconds,
                max_rows=max_rows,
                grain=metric.grain,
                impl_id=impl.id,
            )
            detail_file = None
        results[impl.id] = ImplEvaluation(
            impl_id=impl.id,
            source=impl.source,
            sql_file=impl.sql_file,
            values=values,
            row_count=row_count,
            duration_ms=round(duration, 3),
            detail_file=detail_file,
        )
    return results


def _cleanup_detail_files(evaluations: dict[str, ImplEvaluation]) -> None:
    for item in evaluations.values():
        if item.detail_file:
            try:
                os.unlink(item.detail_file)
            except OSError:
                pass
            item.detail_file = None


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
    chunk_size: int = 2048,
    max_representatives: int = 5,
    spill_root: str | None = None,
) -> tuple[ComparisonResult, dict[str, ImplEvaluation], dict[str, Any]]:
    """Evaluate all implementations, compare dimensions and attribute clusters.

    Returns the dimension comparison, the per-implementation evaluations and a
    standalone attribution block.  Attribution failure never propagates: the
    block is marked ``degraded`` while the dimension comparison stays intact.
    """
    natural_keys = list(metric.natural_keys)
    if not natural_keys:
        evaluations = evaluate_metric_impls(
            metric,
            version,
            catalog,
            sql_roots,
            timeout_seconds=timeout_seconds,
            max_rows=max_rows,
            capture_detail=False,
        )
        comparison = compare_impl_results(
            {impl_id: item.values for impl_id, item in evaluations.items()},
            metric_key=metric.key,
            metric_version=version,
            abs_tolerance=abs_tolerance,
            rel_tolerance=rel_tolerance,
        )
        return comparison, evaluations, skipped_attribution()

    work_dir = tempfile.mkdtemp(prefix="caliber-run-", dir=spill_root)
    try:
        evaluations = evaluate_metric_impls(
            metric,
            version,
            catalog,
            sql_roots,
            timeout_seconds=timeout_seconds,
            max_rows=max_rows,
            capture_detail=True,
            spill_dir=work_dir,
        )
        comparison = compare_impl_results(
            {impl_id: item.values for impl_id, item in evaluations.items()},
            metric_key=metric.key,
            metric_version=version,
            abs_tolerance=abs_tolerance,
            rel_tolerance=rel_tolerance,
        )
        detail_files = {
            impl_id: item.detail_file
            for impl_id, item in evaluations.items()
            if item.detail_file
        }
        if len(detail_files) != len(evaluations):
            attribution = degraded_from_error(
                natural_keys,
                [impl.id for impl in metric.impls],
                EvaluationError("部分实现未产生明细暂存文件"),
            )
        else:
            try:
                attribution = attribute_differences(
                    detail_files,
                    natural_keys,
                    abs_tolerance=abs_tolerance,
                    rel_tolerance=rel_tolerance,
                    chunk_size=chunk_size,
                    max_representatives=max_representatives,
                    spill_root=work_dir,
                )
            except Exception as exc:  # 归因失败不拖垮维度比对
                attribution = degraded_from_error(
                    natural_keys,
                    [impl.id for impl in metric.impls],
                    exc,
                )
        return comparison, evaluations, attribution
    finally:
        bound = locals().get("evaluations")
        if bound is not None:
            _cleanup_detail_files(bound)
        import shutil

        shutil.rmtree(work_dir, ignore_errors=True)
