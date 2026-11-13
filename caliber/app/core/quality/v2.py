"""v2 模式：纯 dqkit 2.x 新语义（兼容适配层关闭后的目标形态）。"""
from __future__ import annotations

from typing import Any, Mapping

import dqkit

from ...errors import EvaluationError
from ...models import ComparisonResult, DimensionDiff
from . import MODE_V2, check_engine_version, engine_meta
from .base import QualitySemantics

_OUTCOME_TO_VERDICT = {"pass": "consistent", "attention": "partial", "fail": "inconsistent"}


class V2Semantics(QualitySemantics):
    """新语义：空值标记后继续、开区间阈值、2.x 原生接口形态。"""

    mode = MODE_V2

    def __init__(self) -> None:
        check_engine_version()

    def engine_meta(self) -> dict[str, Any]:
        return engine_meta()

    def semantics_applied(self) -> dict[str, str]:
        return {
            "null_handling": "flag_and_continue (dqkit v2 native)",
            "threshold_boundary": "exclusive (dqkit v2 native)",
        }

    def assess_value(self, raw: Any, *, impl_id: str) -> float | None:
        try:
            return dqkit.assess_value(raw).value
        except dqkit.DqkitTypeError as exc:
            raise EvaluationError(
                f"实现 {impl_id} 返回的指标值不是数值: {raw!r}",
                details={"impl_id": impl_id},
            ) from exc

    def pair_match(
        self, left: float, right: float, abs_tolerance: float, rel_tolerance: float
    ) -> bool:
        outcome = dqkit.compare_pair(
            left, right, dqkit.Threshold(absolute=abs_tolerance, relative=rel_tolerance)
        )
        return outcome.match

    def compare_impls(
        self,
        results: Mapping[str, Mapping[str, float | None]],
        *,
        metric_key: str,
        metric_version: int,
        abs_tolerance: float,
        rel_tolerance: float,
    ) -> tuple[ComparisonResult, list[dict[str, Any]]]:
        report = dqkit.check(
            dqkit.CheckRequest(
                impl_values=results,
                threshold=dqkit.Threshold(
                    absolute=abs_tolerance, relative=rel_tolerance
                ),
            )
        )
        diffs: list[DimensionDiff] = []
        for finding in report.findings:
            if finding.type == "value_gap":
                diffs.append(
                    DimensionDiff(
                        dimension=finding.slice,
                        kind="value_mismatch",
                        values=dict(finding.values),
                        max_delta=finding.margin,
                        detail="数值超出容差（dqkit v2 开区间判定）",
                    )
                )
            elif finding.type == "null_value":
                diffs.append(
                    DimensionDiff(
                        dimension=finding.slice,
                        kind="null_value",
                        values=dict(finding.values),
                        max_delta=None,
                        detail=(
                            "以下实现返回空值（dqkit v2 标记后继续）: "
                            + ", ".join(finding.impls)
                        ),
                    )
                )
            else:  # missing_slice
                diffs.append(
                    DimensionDiff(
                        dimension=finding.slice,
                        kind="missing_dimension",
                        values=dict(finding.values),
                        max_delta=None,
                        detail=f"以下实现缺少该维度: {', '.join(finding.impls)}",
                    )
                )
        stats = report.stats
        summary = {
            "impl_count": stats["impl_count"],
            "dimension_count": stats["slices_total"],
            "consistent_dimensions": stats["slices_clean"],
            "value_mismatches": stats["value_gaps"],
            "missing_dimensions": stats["missing_slices"],
            "null_values": stats["null_values"],
            "diff_count": len(diffs),
        }
        result = ComparisonResult(
            metric_key=metric_key,
            metric_version=int(metric_version),
            impl_ids=list(results),
            verdict=_OUTCOME_TO_VERDICT[report.outcome],
            dimensions=list(report.slices),
            diffs=diffs,
            summary=summary,
            abs_tolerance=float(abs_tolerance),
            rel_tolerance=float(rel_tolerance),
        )
        return result, []
