"""Metric import service: parse, validate, version and audit."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from ..audit import log_audit, record_error
from ..errors import CaliberError, UnsafeSQLError, ValidationError
from ..models import ImportResult, ImportSummary, MetricDefinition, ValidationIssue
from .dsl_parser import parse_metric_file
from .dsl_validator import resolve_sql_path, validate_definition
from .sql_gate import GATE_META, AccessScope, SqlGate
from .versioning import append_version


class MetricRegistry:
    def __init__(
        self,
        conn: sqlite3.Connection,
        sql_roots: tuple[Path, ...] | list[Path],
        gate: SqlGate | None = None,
    ) -> None:
        self.conn = conn
        self.sql_roots = tuple(Path(root) for root in sql_roots)
        # 默认无 schema 连接 → degraded 前缀/结构校验；CLI 有数据目录时
        # 会传入带 catalog 只读连接的完整模式门禁。
        self.gate = gate or SqlGate(None)

    def _gate_definition(
        self, metric: MetricDefinition
    ) -> tuple[list[ValidationIssue], dict[str, Any]]:
        """对指标下每条实现 SQL 跑只读门禁，返回字段级问题与批次统计。"""
        issues: list[ValidationIssue] = []
        stats = {
            "mode": self.gate.mode,
            "inspected": 0,
            "rejected": 0,
            "whitelisted": 0,
            "degraded": 0,
        }
        if self.gate.mode == "degraded":
            stats["degraded"] = len(metric.impls)
        for index, impl in enumerate(metric.impls):
            field = f"impls[{index}].sql_file"
            try:
                sql_path = resolve_sql_path(impl.sql_file, self.sql_roots)
            except (UnsafeSQLError, CaliberError):
                # 路径问题已由 dsl_validator 记录，门禁阶段跳过。
                continue
            try:
                sql = sql_path.read_text(encoding="utf-8-sig")
            except OSError as exc:
                issues.append(
                    ValidationIssue(field, "sql_unreadable", f"无法读取 SQL 文件: {exc}")
                )
                continue
            stats["inspected"] += 1
            decision = self.gate.inspect_text(
                sql,
                access_scope=AccessScope.from_raw(impl.access),
                allowlist=impl.allowlist,
                context={
                    "metric_key": metric.key,
                    "impl_id": impl.id,
                    "sql_file": impl.sql_file,
                    "stage": "import",
                },
            )
            for passed in decision.whitelisted:
                stats["whitelisted"] += 1
                log_audit(
                    self.conn,
                    "sql_gate.whitelist",
                    f"{metric.key}/{impl.id}",
                    "ok",
                    {
                        "stage": "import",
                        "metric_key": metric.key,
                        "impl_id": impl.id,
                        "sql_file": impl.sql_file,
                        "entry": passed.to_dict(),
                        **decision.audit_summary(),
                    },
                )
            if not decision.allowed:
                stats["rejected"] += 1
                for violation in decision.violations:
                    issues.append(
                        ValidationIssue(
                            field,
                            "unsafe_sql",
                            (
                                f"实现 {impl.id} 只读门禁拒绝"
                                f"（语句类型 {violation.statement_type or 'UNKNOWN'}，"
                                f"规则 {violation.rule}）: {violation.message}"
                            ),
                        )
                    )
                log_audit(
                    self.conn,
                    "sql_gate.rejected",
                    f"{metric.key}/{impl.id}",
                    "rejected",
                    {
                        "stage": "import",
                        "metric_key": metric.key,
                        "impl_id": impl.id,
                        "sql_file": impl.sql_file,
                        "reason": decision.rejection_reason(),
                        "gate": decision.to_dict(),
                    },
                )
        return issues, stats

    def import_definition(
        self, metric: MetricDefinition, source_file: str = "<inline>"
    ) -> int:
        issues = validate_definition(metric, self.sql_roots)
        gate_issues, _ = self._gate_definition(metric)
        issues.extend(gate_issues)
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
        gate_stats = {
            "mode": self.gate.mode,
            "inspected": 0,
            "rejected": 0,
            "whitelisted": 0,
            "degraded": 0,
            "gate_meta": GATE_META,
        }
        seen: set[str] = set()
        for file_path in files:
            try:
                metric = parse_metric_file(file_path)
                if metric.key in seen:
                    raise CaliberError(f"同一批次内 key 重复: {metric.key}")
                seen.add(metric.key)
                issues = validate_definition(metric, self.sql_roots)
                gate_issues, file_stats = self._gate_definition(metric)
                for name in ("inspected", "rejected", "whitelisted", "degraded"):
                    gate_stats[name] += file_stats[name]
                issues.extend(gate_issues)
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
                    {"version": version, "source_file": str(file_path),
                     "gate_mode": self.gate.mode},
                )
            except CaliberError as exc:
                message = str(exc)
                details = exc.details
                # 摘要里精确定位文件；details 内保留 impl id / 语句类型 / 规则。
                summary.errors.append(
                    {
                        "file": str(file_path),
                        "error": exc.code,
                        "message": message,
                        "details": details if isinstance(details, list) else [],
                    }
                )
                record_error(self.conn, "metric.import", str(file_path), exc)
        summary.gate_stats = gate_stats
        return summary
