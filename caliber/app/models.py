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
    errors: list[dict[str, Any]] = field(default_factory=list)

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

