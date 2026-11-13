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
    # 可选访问范围声明（{"tables": [...], "sqlite_master": bool}）。
    # 老 YAML 不带该字段时为 None，门禁降级为全量校验而不报错。
    access: dict[str, Any] | None = None
    # 显式白名单：误报可接受时按 rule/statement_type/table 放行并写审计。
    allowlist: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "ImplementDef":
        access = data.get("access")
        allowlist = data.get("allowlist", [])
        return cls(
            id=str(data.get("id", "")),
            source=str(data.get("source", "")),
            sql_file=str(data.get("sql_file", "")),
            access=dict(access) if isinstance(access, Mapping) else None,
            allowlist=[dict(item) for item in allowlist if isinstance(item, Mapping)],
        )


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
            ImplementDef.from_mapping(item)
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
    # 安全门禁批次统计：检查的实现数、拒绝数、白名单放行数、降级次数。
    gate_stats: dict[str, Any] = field(default_factory=dict)

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
    # 只读安全门禁结果。rejected=True 时该实现未被执行，values 为空，
    # rejection_reason 记录规则、语句类型、证据，供快照与审计定位。
    rejected: bool = False
    rejection_reason: dict[str, Any] | None = None
    gate_mode: str | None = None
    sql_sha256: str | None = None
    whitelisted: bool = False

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

