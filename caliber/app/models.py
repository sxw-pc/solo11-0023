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
    # Content fingerprint captured at import time and embedded in the stored
    # version. Empty for definitions parsed from YAML or pre-fingerprint rows.
    sql_fingerprint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


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
                sql_fingerprint=str(item.get("sql_fingerprint", "") or ""),
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
    preview: dict[str, Any] | None = None


@dataclass(slots=True)
class ImportSummary:
    imported: list[ImportResult] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)
    previews: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def high_risk_count(self) -> int:
        return sum(1 for item in self.previews if item.get("high_risk"))


@dataclass(slots=True)
class ImpactPreview:
    """Read-only pre-publish impact assessment for one candidate definition.

    The structure is plain JSON-friendly data so it can travel inside import
    summaries, CLI output and HTTP responses without extra encoding logic.
    """

    key: str
    source_file: str
    exists: bool
    current_version: int | None
    candidate_version: int | None
    changed: bool
    high_risk: bool
    degraded: bool = False
    risk_reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    added_impls: list[str] = field(default_factory=list)
    removed_impls: list[str] = field(default_factory=list)
    modified_impls: list[dict[str, Any]] = field(default_factory=list)
    sql_fingerprint_changes: list[dict[str, Any]] = field(default_factory=list)
    grain_changed: bool = False
    grain_before: str | None = None
    grain_after: str | None = None
    unit_changed: bool = False
    unit_before: str | None = None
    unit_after: str | None = None
    affected_run_ids: list[int] = field(default_factory=list)
    affected_snapshot_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


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

