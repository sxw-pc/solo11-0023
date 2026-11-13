"""Plain data objects used by the power assessment module.

The objects here are intentionally self-contained: no SQLite row, Flask
request or existing comparison-report object is reused.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class Issue:
    """A validation error or a degradation warning.

    ``severity`` is ``"error"`` for fatal artifact problems and ``"warning"``
    for cases where the assessment can continue with a documented downgrade.
    """

    code: str
    message: str
    severity: str = "warning"
    artifact: str | None = None
    dataset: str | None = None
    rule: str | None = None
    detail: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
        }
        for key in ("artifact", "dataset", "rule", "detail"):
            value = getattr(self, key)
            if value is not None:
                payload[key] = dict(value) if key == "detail" else value
        return payload

    @property
    def sort_key(self) -> tuple[str, ...]:
        return (
            self.severity,
            self.code,
            self.artifact or "",
            self.dataset or "",
            self.rule or "",
            self.message,
        )


@dataclass(frozen=True, slots=True)
class RuleObservation:
    """One rule's counts on one dataset."""

    artifact: str
    dataset: str
    rule_key: str
    name: str | None
    hits: int
    passes: int
    total: int

    @property
    def hit_rate(self) -> float:
        return self.hits / self.total

    @property
    def pass_rate(self) -> float:
        return self.passes / self.total

    def counts(self) -> tuple[int, int, int]:
        return (self.hits, self.passes, self.total)

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact": self.artifact,
            "dataset": self.dataset,
            "hits": self.hits,
            "passes": self.passes,
            "total": self.total,
            "hit_rate": self.hit_rate,
            "pass_rate": self.pass_rate,
        }


@dataclass(slots=True)
class ArtifactMeta:
    """Summary of one successfully parsed input artifact."""

    path: str
    digest: str
    dataset_count: int
    rule_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "sha256": self.digest,
            "dataset_count": self.dataset_count,
            "rule_count": self.rule_count,
        }


@dataclass(slots=True)
class LoadedArtifacts:
    """Normalized observations plus explicit errors and degradation notes.

    ``issues`` carries every problem the loader detected.  ``severity ==
    "error"`` marks records or artifacts that were rejected; nothing is ever
    skipped without an accompanying issue.
    """

    observations: list[RuleObservation] = field(default_factory=list)
    artifacts: list[ArtifactMeta] = field(default_factory=list)
    dataset_aliases: dict[str, list[str]] = field(default_factory=dict)
    rule_aliases: dict[str, list[str]] = field(default_factory=dict)
    issues: list[Issue] = field(default_factory=list)

    @property
    def errors(self) -> list[Issue]:
        return [item for item in self.issues if item.severity == "error"]

    @property
    def warnings(self) -> list[Issue]:
        return [item for item in self.issues if item.severity != "error"]


@dataclass(frozen=True, slots=True)
class AssessmentSettings:
    """User-controlled statistical parameters."""

    alpha: float = 0.05
    power: float = 0.80
    min_effect: float = 0.05
    min_expected_cell: float = 5.0
    min_group_total: int = 30
    correction: str = "holm"
    exact_state_budget: int = 500_000
    confidence_level: float = 0.95

    def __post_init__(self) -> None:
        if not 0.0 < self.alpha < 1.0:
            raise ValueError(f"alpha 必须严格位于 (0, 1) 内，实际为 {self.alpha!r}")
        if not 0.0 < self.power < 1.0:
            raise ValueError(f"power 必须严格位于 (0, 1) 内，实际为 {self.power!r}")
        if not 0.0 < self.min_effect <= 1.0:
            raise ValueError(
                f"min_effect 必须位于 (0, 1] 内，实际为 {self.min_effect!r}"
            )
        if self.min_expected_cell < 0.0:
            raise ValueError("min_expected_cell 不能为负数")
        if self.min_group_total <= 0:
            raise ValueError("min_group_total 必须大于 0")
        if self.exact_state_budget <= 0:
            raise ValueError("exact_state_budget 必须大于 0")
        if not 0.0 < self.confidence_level < 1.0:
            raise ValueError("confidence_level 必须严格位于 (0, 1) 内")
        if self.correction not in {"holm", "none"}:
            raise ValueError(f"不支持的校正方法: {self.correction!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "alpha": self.alpha,
            "power": self.power,
            "min_effect": self.min_effect,
            "min_expected_cell": self.min_expected_cell,
            "min_group_total": self.min_group_total,
            "correction": self.correction,
            "exact_state_budget": self.exact_state_budget,
            "confidence_level": self.confidence_level,
        }
