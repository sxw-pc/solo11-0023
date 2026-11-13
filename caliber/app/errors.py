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


class EvaluationRowLimit(EvaluationError):
    """Row cap hit.

    ``observed_rows`` is the number of rows actually read before evaluation
    stopped.  ``row_count_exact`` is false when the remaining rows could not
    be drained (for example because the metric-level timeout expired during
    counting), in which case ``observed_rows`` is a lower bound.
    """

    code = "row_limit_exceeded"

    def __init__(
        self,
        message: str,
        *,
        impl_id: str,
        max_rows: int,
        observed_rows: int,
        row_count_exact: bool,
        elapsed_ms: float = 0.0,
    ) -> None:
        super().__init__(
            message,
            details={
                "impl_id": impl_id,
                "max_rows": int(max_rows),
                "observed_rows": int(observed_rows),
                "row_count_exact": bool(row_count_exact),
                "elapsed_ms": round(float(elapsed_ms), 3),
            },
        )
        self.impl_id = impl_id
        self.max_rows = int(max_rows)
        self.observed_rows = int(observed_rows)
        self.row_count_exact = bool(row_count_exact)
        self.elapsed_ms = float(elapsed_ms)


class ReportError(CaliberError):
    code = "report_error"


class RunError(CaliberError):
    code = "run_error"
