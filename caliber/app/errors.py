"""Typed errors used across the application.

Every public entry point translates expected failures into one of these
exceptions.  The Flask layer and the CLI turn them into structured responses
and always write an audit record.
"""
from __future__ import annotations

from typing import Any


class CaliberError(Exception):
    """Base class for all expected domain errors."""

    code = "caliber_error"
    http_status = 400

    def __init__(
        self,
        message: str,
        *,
        details: Any | None = None,
        code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.details = details
        if code:
            self.code = code

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"error": self.code, "message": self.message}
        if self.details is not None:
            payload["details"] = self.details
        return payload


class DslParseError(CaliberError):
    code = "dsl_parse_error"


class ValidationError(CaliberError):
    code = "validation_error"

    def __init__(self, issues: list[dict[str, str]]) -> None:
        self.issues = issues
        message = "; ".join(item.get("message", "invalid metric") for item in issues)
        super().__init__(message or "指标定义校验失败", details=issues)


class NotFoundError(CaliberError):
    code = "not_found"
    http_status = 404


class DatasetError(CaliberError):
    code = "dataset_error"


class UnsafeSQLError(CaliberError):
    code = "unsafe_sql"


class EvaluationError(CaliberError):
    code = "evaluation_error"


class EvaluationTimeout(EvaluationError):
    code = "evaluation_timeout"


class ReportError(CaliberError):
    code = "report_error"


class RunError(CaliberError):
    code = "run_error"


class DatabaseVersionError(CaliberError):
    """Raised when the SQLite engine is below the configured version floor."""

    code = "unsupported_sqlite_version"
    http_status = 500


class MigrationError(CaliberError):
    """Raised when a schema migration fails; the database is rolled back."""

    code = "migration_failed"
    http_status = 500
