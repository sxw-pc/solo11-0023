"""Metric import service: parse, validate, version and audit."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from ..audit import log_audit, record_error
from ..errors import CaliberError, ValidationError
from ..models import ImportResult, ImportSummary, MetricDefinition
from .dsl_parser import parse_metric_file
from .dsl_validator import validate_definition
from .versioning import append_version


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
            raise ValidationError([issue.to_dict() for issue in issues])
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
                message = str(exc)
                details = exc.details
                summary.errors.append(
                    {"file": str(file_path), "error": exc.code, "message": message,
                     "details": details if isinstance(details, list) else []}
                )
                record_error(self.conn, "metric.import", str(file_path), exc)
        return summary
