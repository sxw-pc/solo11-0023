"""Metric import service: parse, validate, version and audit."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from ..audit import log_audit, record_error
from ..errors import CaliberError, ImpactBlockedError, ValidationError
from ..models import (
    ImpactPreview,
    ImportResult,
    ImportSummary,
    MetricDefinition,
)
from .dsl_parser import parse_metric_file
from .dsl_validator import validate_definition
from .impact import build_impact_preview, fingerprint_definition
from .versioning import append_version


class MetricRegistry:
    def __init__(
        self,
        conn: sqlite3.Connection,
        sql_roots: tuple[Path, ...] | list[Path],
        *,
        strict: bool = False,
    ) -> None:
        self.conn = conn
        self.sql_roots = tuple(Path(root) for root in sql_roots)
        # Default (lenient) mode previews but never blocks, preserving the
        # historical import behaviour; strict mode rejects high-risk changes.
        self.strict = bool(strict)

    # ------------------------------------------------------------------
    # Read-only impact preview
    # ------------------------------------------------------------------
    def preview_definition(
        self, metric: MetricDefinition, source_file: str = "<inline>"
    ) -> ImpactPreview:
        """Return the impact preview without writing anything to the registry."""
        return build_impact_preview(
            self.conn, metric, self.sql_roots, source_file=source_file
        )

    def preview_file(self, path: str | Path) -> dict[str, Any]:
        file_path = Path(path)
        metric = parse_metric_file(file_path)
        # Pure read-only preview: validation issues are reported alongside the
        # verdict but never raise, so a high-risk shrink is visible even when
        # the candidate would independently fail the min-items rule.
        issues = validate_definition(metric, self.sql_roots)
        preview = self.preview_definition(metric, str(file_path))
        payload = preview.to_dict()
        payload["validation_issues"] = [item.to_dict() for item in issues]
        return payload

    def preview_directory(self, directory: str | Path) -> list[dict[str, Any]]:
        root = Path(directory)
        if not root.is_dir():
            raise CaliberError(f"指标目录不存在: {root}")
        files = sorted([*root.glob("*.yaml"), *root.glob("*.yml")])
        if not files:
            raise CaliberError(f"指标目录中没有 YAML 文件: {root}")
        previews: list[dict[str, Any]] = []
        seen: set[str] = set()
        for file_path in files:
            try:
                metric = parse_metric_file(file_path)
                if metric.key in seen:
                    raise CaliberError(f"同一批次内 key 重复: {metric.key}")
                seen.add(metric.key)
                previews.append(self.preview_file(file_path))
            except CaliberError as exc:
                previews.append(
                    {
                        "key": None,
                        "source_file": str(file_path),
                        "exists": False,
                        "changed": False,
                        "high_risk": False,
                        "degraded": False,
                        "error": {
                            "code": exc.code,
                            "message": str(exc),
                            "details": exc.details
                            if isinstance(exc.details, list)
                            else [],
                        },
                    }
                )
        return previews

    # ------------------------------------------------------------------
    # Importing
    # ------------------------------------------------------------------
    def _enforce_preview(self, preview: ImpactPreview) -> None:
        if self.strict and preview.high_risk:
            detail = preview.to_dict()
            # Blocking writes an audit record but must not create a version.
            log_audit(
                self.conn,
                "metric.import.blocked",
                preview.key,
                "blocked",
                detail,
            )
            raise ImpactBlockedError(
                "严格模式下高风险变更已拦截: "
                + ", ".join(preview.risk_reasons),
                details=detail,
            )

    def _import_with_preview(
        self, metric: MetricDefinition, source_file: str
    ) -> tuple[int, ImpactPreview]:
        # Freeze each SQL file's content fingerprint into the definition that
        # will be stored, so future previews can compare against an immutable
        # historical fingerprint rather than a mutable on-disk file.
        stored_metric, fingerprint_warnings = fingerprint_definition(
            metric, self.sql_roots
        )
        # Build the read-only preview and enforce strict blocking *before*
        # ordinary validation: a high-risk shrink must be intercepted even when
        # the candidate would also fail the min-items rule. In lenient mode the
        # preview never raises, so validation behaviour is unchanged.
        preview = self.preview_definition(stored_metric, source_file)
        for warning in fingerprint_warnings:
            if warning not in preview.warnings:
                preview.warnings.append(warning)
        self._enforce_preview(preview)
        issues = validate_definition(metric, self.sql_roots)
        if issues:
            raise ValidationError([issue.to_dict() for issue in issues])
        version = append_version(self.conn, stored_metric, source_file)
        log_audit(
            self.conn,
            "metric.import",
            metric.key,
            "ok",
            {
                "version": version,
                "source_file": source_file,
                "high_risk": preview.high_risk,
                "risk_reasons": preview.risk_reasons,
                "degraded": preview.degraded,
            },
        )
        return version, preview

    def import_definition(
        self, metric: MetricDefinition, source_file: str = "<inline>"
    ) -> int:
        version, _preview = self._import_with_preview(metric, source_file)
        return version

    def import_file(self, path: str | Path) -> ImportResult:
        file_path = Path(path)
        metric = parse_metric_file(file_path)
        version, preview = self._import_with_preview(metric, str(file_path))
        return ImportResult(
            key=metric.key,
            name=metric.name,
            version=version,
            source_file=str(file_path),
            preview=preview.to_dict(),
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
                version, preview = self._import_with_preview(metric, str(file_path))
                summary.imported.append(
                    ImportResult(
                        metric.key,
                        metric.name,
                        version,
                        str(file_path),
                        preview=preview.to_dict(),
                    )
                )
                summary.previews.append(preview.to_dict())
            except ImpactBlockedError as exc:
                detail = exc.details if isinstance(exc.details, dict) else {}
                summary.errors.append(
                    {
                        "file": str(file_path),
                        "error": exc.code,
                        "message": exc.message,
                        "details": exc.details if isinstance(exc.details, list) else [],
                    }
                )
                if detail:
                    summary.previews.append(detail)
                # The dedicated ``metric.import.blocked`` audit row was already
                # written by _enforce_preview; do not double-record here.
            except CaliberError as exc:
                message = str(exc)
                details = exc.details
                summary.errors.append(
                    {"file": str(file_path), "error": exc.code, "message": message,
                     "details": details if isinstance(details, list) else []}
                )
                record_error(self.conn, "metric.import", str(file_path), exc)
        return summary
