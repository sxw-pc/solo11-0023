"""Metric import service: parse, validate, version and audit."""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any

from ..audit import log_audit, record_error
from ..errors import CaliberError, ValidationError
from ..models import ImportResult, ImportSummary, MetricDefinition
from .dsl_parser import parse_metric_file
from .dsl_validator import validate_definition
from .versioning import append_version, candidate_version

IMPL_FIELD_RE = re.compile(r"^impls\[(\d+)\]")


def _impl_ids_for_issues(
    metric: MetricDefinition | None, details: Any
) -> list[str]:
    """Map field-level issue paths back to the offending implementation ids."""
    if not isinstance(details, list):
        return []
    found: list[str] = []
    for item in details:
        if not isinstance(item, dict):
            continue
        field = str(item.get("field", ""))
        match = IMPL_FIELD_RE.match(field)
        impl_id = item.get("impl_id")
        if not impl_id and match and metric is not None:
            index = int(match.group(1))
            if 0 <= index < len(metric.impls):
                impl_id = metric.impls[index].id
        if impl_id and impl_id not in found:
            found.append(str(impl_id))
    return found


def _enrich_issues(
    metric: MetricDefinition, issues: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Attach metric key and impl id to each field-level validation issue."""
    enriched: list[dict[str, Any]] = []
    for item in issues:
        if not isinstance(item, dict):
            enriched.append(item)
            continue
        detail = dict(item)
        detail.setdefault("metric_key", metric.key)
        match = IMPL_FIELD_RE.match(str(detail.get("field", "")))
        if match and not detail.get("impl_id"):
            index = int(match.group(1))
            if 0 <= index < len(metric.impls):
                detail["impl_id"] = metric.impls[index].id
        enriched.append(detail)
    return enriched


def _current_version(conn: sqlite3.Connection, key: str) -> int | None:
    row = conn.execute(
        "SELECT current_version FROM metrics WHERE key = ?", (key,)
    ).fetchone()
    return int(row["current_version"]) if row is not None else None


class MetricRegistry:
    def __init__(
        self,
        conn: sqlite3.Connection,
        sql_roots: tuple[Path, ...] | list[Path],
    ) -> None:
        self.conn = conn
        self.sql_roots = tuple(Path(root) for root in sql_roots)

    def import_definition(
        self, metric: MetricDefinition, source_file: str = "<inline>"
    ) -> int:
        issues = validate_definition(metric, self.sql_roots)
        if issues:
            raise ValidationError(_enrich_issues(
                metric, [issue.to_dict() for issue in issues]
            ))
        version = append_version(self.conn, metric, source_file)
        log_audit(
            self.conn,
            "metric.import",
            metric.key,
            "ok",
            {"version": version, "source_file": source_file},
        )
        return version

    def import_file(self, path: str | Path) -> ImportResult:
        file_path = Path(path)
        metric = parse_metric_file(file_path)
        version = self.import_definition(metric, str(file_path))
        return ImportResult(
            key=metric.key,
            name=metric.name,
            version=version,
            source_file=str(file_path),
        )

    def _record_import_failure(
        self,
        summary: ImportSummary,
        file_path: Path,
        exc: CaliberError,
        metric: MetricDefinition | None,
    ) -> None:
        """Write a diagnostic that identifies key, version and impl id.

        A failed file never reaches ``append_version``, so the metric's
        existing ``current_version`` and history stay untouched.
        """
        details = exc.details if isinstance(exc.details, list) else []
        if metric is not None:
            details = _enrich_issues(metric, details)
        key = metric.key if metric is not None else None
        impl_ids = _impl_ids_for_issues(metric, details)
        current = _current_version(self.conn, key) if key else None
        attempted = candidate_version(self.conn, metric) if metric is not None else None
        fields = sorted(
            {str(item.get("field")) for item in details
             if isinstance(item, dict) and item.get("field")}
        )
        summary.errors.append(
            {
                "file": str(file_path),
                "error": exc.code,
                "message": str(exc),
                "details": details,
                "metric_key": key,
                "version": attempted,
                "current_version": current,
                "impl_ids": impl_ids,
                "fields": fields,
            }
        )
        record_error(
            self.conn,
            "metric.import",
            str(file_path),
            exc,
            extra={
                "metric_key": key,
                "attempted_version": attempted,
                "current_version": current,
                "impl_ids": impl_ids,
                "fields": fields,
                "details": details,
            },
        )

    def import_directory(self, directory: str | Path) -> ImportSummary:
        root = Path(directory)
        summary = ImportSummary()
        if not root.is_dir():
            raise CaliberError(f"指标目录不存在: {root}")
        files = sorted([*root.glob("*.yaml"), *root.glob("*.yml")])
        if not files:
            raise CaliberError(f"指标目录中没有 YAML 文件: {root}")
        seen: set[str] = set()
        for file_path in files:
            metric: MetricDefinition | None = None
            try:
                metric = parse_metric_file(file_path)
                if metric.key in seen:
                    raise CaliberError(f"同一批次内 key 重复: {metric.key}")
                seen.add(metric.key)
                issues = validate_definition(metric, self.sql_roots)
                if issues:
                    raise ValidationError([item.to_dict() for item in issues])
                version = append_version(self.conn, metric, str(file_path))
                summary.imported.append(
                    ImportResult(metric.key, metric.name, version, str(file_path))
                )
                log_audit(
                    self.conn,
                    "metric.import",
                    metric.key,
                    "ok",
                    {"version": version, "source_file": str(file_path)},
                )
            except CaliberError as exc:
                self._record_import_failure(summary, file_path, exc, metric)
        return summary
