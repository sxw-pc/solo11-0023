"""Plain data objects shared by the parser, registry, evaluator and reporter."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(slots=True)
class ImplementDef:
    id: str
    source: str
    sql_file: str


@dataclass(slots=True)
class MetricDefinition:
    key: str
    name: str
    owner: str
    description: str
    unit: str
    grain: str
    impls: list[ImplementDef]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "MetricDefinition":
        impls = [
            ImplementDef(
                id=str(item.get("id", "")),
                source=str(item.get("source", "")),
                sql_file=str(item.get("sql_file", "")),
            )
            for item in data.get("impls", [])
            if isinstance(item, Mapping)
        ]
        return cls(
            key=str(data.get("key", "")),
            name=str(data.get("name", "")),
            owner=str(data.get("owner", "")),
            description=str(data.get("description", "")),
            unit=str(data.get("unit", "")),
            grain=str(data.get("grain", "")),
            impls=impls,
        )


@dataclass(slots=True)
class ValidationIssue:
    field: str
    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"field": self.field, "code": self.code, "message": self.message}


@dataclass(slots=True)
class ImportResult:
    key: str
    name: str
    version: int
    source_file: str


@dataclass(slots=True)
class ImportSummary:
    imported: list[ImportResult] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass(slots=True)
class DatasetColumn:
    name: str
    type: str


@dataclass(slots=True)
class DatasetMeta:
    key: str
    path: str
    encoding: str
    row_count: int
    column_count: int
    columns: list[DatasetColumn]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ImplEvaluation:
    impl_id: str
    source: str
    sql_file: str
    values: dict[str, float]
    row_count: int
    duration_ms: float
    # Degradation markers.  Normal evaluations keep the historical defaults,
    # so old report consumers see exactly the same JSON as before.
    status: str = "ok"
    error_code: str | None = None
    message: str = ""
    row_count_exact: bool = True
    max_rows: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class DimensionDiff:
    dimension: str
    kind: str
    values: dict[str, float | None]
    max_delta: float | None = None
    detail: str = ""


@dataclass(slots=True)
class ComparisonResult:
    metric_key: str
    metric_version: int
    impl_ids: list[str]
    verdict: str
    dimensions: list[str]
    diffs: list[DimensionDiff]
    summary: dict[str, Any]
    abs_tolerance: float
    rel_tolerance: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class MetricRunOutcome:
    """Result of evaluating one metric inside a batch run.

    A degraded outcome is *not* a comparison verdict: no trustworthy
    comparison exists, so ``comparison`` is ``None`` and the snapshot verdict
    uses the reserved ``degraded`` value with ``degraded_reason`` set to
    ``timeout`` or ``row_limit``.

    Degraded snapshots are persisted so the report/dashboard can still show
    that the metric was attempted, but every degraded artifact carries an
    explicit marker (``verdict == "degraded"``, ``summary.degraded == true``
    and ``result.degraded``) so downstream aggregation must filter them out
    before treating any value as a comparison conclusion.
    """

    metric: MetricDefinition
    version: int
    status: str
    comparison: ComparisonResult | None
    evaluations: dict[str, ImplEvaluation]
    degraded_reason: str | None = None
    error_code: str | None = None
    message: str = ""
    failed_impl_id: str | None = None
    timeout_seconds: float | None = None
    max_rows: int | None = None
    observed_rows: int | None = None
    row_count_exact: bool | None = None

    @property
    def degraded(self) -> bool:
        return self.status == "degraded"

    def snapshot_payload(self) -> dict[str, Any]:
        """Build the verdict/summary/result triple stored in one snapshot."""
        if self.comparison is not None:
            result = self.comparison.to_dict()
            result["metric"] = self.metric.to_dict()
            result["implementations"] = [
                item.to_dict() for item in self.evaluations.values()
            ]
            summary = dict(self.comparison.summary)
        else:
            result = {
                "metric": self.metric.to_dict(),
                "verdict": "degraded",
                "degraded": self._degradation(),
                "implementations": [
                    item.to_dict() for item in self.evaluations.values()
                ],
            }
            summary = {
                "degraded": True,
                "degraded_reason": self.degraded_reason,
                "error_code": self.error_code,
                "failed_impl_id": self.failed_impl_id,
                "impl_count": len(self.metric.impls),
                "evaluated_impl_count": len(self.evaluations),
                "dimension_count": 0,
                "diff_count": 0,
                "max_rows": self.max_rows,
                "observed_rows": self.observed_rows,
                "row_count_exact": self.row_count_exact,
            }
        summary = {
            **summary,
            "status": self.status,
            "degraded": self.degraded,
            "degraded_reason": self.degraded_reason,
        }
        result["status"] = self.status
        result["degraded_reason"] = self.degraded_reason
        if self.degraded:
            result["degraded"] = self._degradation()
            verdict = "degraded"
        else:
            verdict = self.comparison.verdict
        return {"verdict": verdict, "summary": summary, "result": result}

    def _degradation(self) -> dict[str, Any]:
        return {
            "status": "degraded",
            "reason": self.degraded_reason,
            "error_code": self.error_code,
            "message": self.message,
            "failed_impl_id": self.failed_impl_id,
            "timeout_seconds": self.timeout_seconds,
            "max_rows": self.max_rows,
            "observed_rows": self.observed_rows,
            "row_count_exact": self.row_count_exact,
        }
