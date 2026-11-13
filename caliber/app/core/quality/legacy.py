"""legacy 模式：冻结的 dqkit 1.4.2 引擎（回退路径）。"""
from __future__ import annotations

from typing import Any, Mapping

from dqkit import v1

from ...errors import EvaluationError, QualityVersionMismatch
from ...models import ComparisonResult, DimensionDiff
from . import MODE_LEGACY
from .base import QualitySemantics


class LegacySemantics(QualitySemantics):
    """完整旧语义：空值即错误、闭区间阈值、旧版返回字段。"""

    mode = MODE_LEGACY

    def __init__(self) -> None:
        if v1.API_VERSION != 1:
            raise QualityVersionMismatch(
                f"数据质量库版本不匹配: 回退模式需要 dqkit v1 引擎（API v1），"
                f"当前 vendored 旧版为 dqkit {v1.__version__}（API v{v1.API_VERSION}）。"
                "请恢复 dqkit/v1.py 的 1.4.2 冻结拷贝。",
                details={
                    "expected_api": 1,
                    "installed_version": v1.__version__,
                    "installed_api": v1.API_VERSION,
                },
            )

    def engine_meta(self) -> dict[str, Any]:
        return {"name": "dqkit", "version": v1.__version__, "api": v1.API_VERSION}

    def semantics_applied(self) -> dict[str, str]:
        return {
            "null_handling": "error_on_null (dqkit v1)",
            "threshold_boundary": "inclusive (dqkit v1)",
        }

    def assess_value(self, raw: Any, *, impl_id: str) -> float | None:
        try:
            return v1.validate_number(raw)
        except (v1.NullValueError, v1.TypeViolationError) as exc:
            raise EvaluationError(
                f"实现 {impl_id} 返回的指标值不是数值: {raw!r}",
                details={"impl_id": impl_id},
            ) from exc

    def pair_match(
        self, left: float, right: float, abs_tolerance: float, rel_tolerance: float
    ) -> bool:
        return v1.within_tolerance(left, right, abs_tolerance, rel_tolerance)

    def compare_impls(
        self,
        results: Mapping[str, Mapping[str, float | None]],
        *,
        metric_key: str,
        metric_version: int,
        abs_tolerance: float,
        rel_tolerance: float,
    ) -> tuple[ComparisonResult, list[dict[str, Any]]]:
        report = v1.summarize(
            results, abs_tolerance=abs_tolerance, rel_tolerance=rel_tolerance
        )
        result = ComparisonResult(
            metric_key=metric_key,
            metric_version=int(metric_version),
            impl_ids=list(report["impl_ids"]),
            verdict=report["verdict"],
            dimensions=list(report["dimensions"]),
            diffs=[DimensionDiff(**item) for item in report["diffs"]],
            summary=dict(report["summary"]),
            abs_tolerance=float(abs_tolerance),
            rel_tolerance=float(rel_tolerance),
        )
        return result, []
