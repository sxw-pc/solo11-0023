"""compat 模式：dqkit 2.x 之上的 v1 兼容适配层（迁移期默认）。

适配层不重新实现旧语义，而是**驱动新引擎并调和其输出**：

* 空值：dqkit 2.x 会把 NULL 标记为 ``null_value`` 继续执行；v1 的契约是
  直接求值失败。适配层在求值阶段把该标记转换回 v1 的错误，并在错误的
  ``details.semantic_differences`` 中记录这次语义调和。
* 阈值：dqkit 2.x 的开区间会把"差值恰好等于阈值"判为不一致；v1 闭区间
  判为一致。适配层检查每个 ``value_gap`` 发现项的成对明细，若全部超差
  对都是 ``boundary_hit``（恰好压线），则按 v1 口径翻回一致，并把该维度
  记入 ``semantic_differences``（类型 ``threshold_boundary``）。
* 返回字段：2.x 的 ``outcome/findings/stats`` 被映射回旧版
  ``verdict/diffs/summary`` 词汇，下游（快照、报告、API）无需改动。

所有调和都写入执行元信息，绝不静默丢弃——下游口径若发生变化，一定能
在 ``execution_meta.semantic_differences`` 里追到具体维度与数值。
"""
from __future__ import annotations

from typing import Any, Mapping

import dqkit

from ...errors import EvaluationError
from ...models import ComparisonResult, DimensionDiff
from . import MODE_COMPAT, check_engine_version, engine_meta
from .base import QualitySemantics


class CompatSemantics(QualitySemantics):
    """在 dqkit 2.x 上复现 v1 判定口径，并记录每一处语义差异。"""

    mode = MODE_COMPAT

    def __init__(self) -> None:
        check_engine_version()

    def engine_meta(self) -> dict[str, Any]:
        return engine_meta()

    def semantics_applied(self) -> dict[str, str]:
        return {
            "null_handling": "error_on_null (v1, 由适配层在 dqkit v2 上模拟)",
            "threshold_boundary": "inclusive (v1, 由适配层调和 v2 开区间结果)",
        }

    def assess_value(self, raw: Any, *, impl_id: str) -> float | None:
        try:
            assessment = dqkit.assess_value(raw)
        except dqkit.DqkitTypeError as exc:
            raise EvaluationError(
                f"实现 {impl_id} 返回的指标值不是数值: {raw!r}",
                details={"impl_id": impl_id},
            ) from exc
        if "null" in assessment.flags:
            difference = {
                "type": "null_value",
                "impl_id": impl_id,
                "v1_handling": "evaluation_error",
                "v2_handling": "flag_and_continue",
                "resolution": "kept_v1",
                "note": "dqkit v2 将空值标记为 null_value 继续执行；"
                "兼容层按 v1 口径转为求值错误",
            }
            raise EvaluationError(
                f"实现 {impl_id} 返回的指标值不是数值: {raw!r}",
                details={
                    "impl_id": impl_id,
                    "semantics_mode": self.mode,
                    "semantic_differences": [difference],
                },
            )
        return assessment.value

    def pair_match(
        self, left: float, right: float, abs_tolerance: float, rel_tolerance: float
    ) -> bool:
        outcome = dqkit.compare_pair(
            left, right, dqkit.Threshold(absolute=abs_tolerance, relative=rel_tolerance)
        )
        # v1 闭区间：恰好压线（boundary_hit）也算一致。
        return outcome.match or outcome.boundary_hit

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
        findings_by_slice: dict[str, list[dqkit.Finding]] = {}
        for finding in report.findings:
            findings_by_slice.setdefault(finding.slice, []).append(finding)

        impl_ids = list(results)
        differences: list[dict[str, Any]] = []
        diffs: list[DimensionDiff] = []
        value_mismatches = 0
        missing_dimensions = 0

        for dimension in report.slices:
            findings = findings_by_slice.get(dimension, [])
            gap = next((f for f in findings if f.type == "value_gap"), None)
            null_finding = next((f for f in findings if f.type == "null_value"), None)
            missing_finding = next(
                (f for f in findings if f.type == "missing_slice"), None
            )
            aligned = {
                impl_id: results[impl_id].get(dimension) for impl_id in impl_ids
            }

            # --- 阈值区间调和：v2 开区间判不一致、v1 闭区间判一致 ---
            real_gap = False
            if gap is not None:
                boundary_pairs = [
                    pair for pair in gap.pairs if not pair.match and pair.boundary_hit
                ]
                real_gap = any(
                    not pair.match and not pair.boundary_hit for pair in gap.pairs
                )
                if not real_gap and boundary_pairs:
                    differences.append(
                        {
                            "type": "threshold_boundary",
                            "dimension": dimension,
                            "pairs": [
                                {
                                    "left": pair.left,
                                    "right": pair.right,
                                    "delta": pair.delta,
                                    "threshold": pair.threshold,
                                }
                                for pair in boundary_pairs
                            ],
                            "v1_outcome": "match",
                            "v2_outcome": "mismatch",
                            "resolution": "kept_v1",
                            "note": "差值恰好等于阈值：v2 开区间判不一致，"
                            "兼容层按 v1 闭区间口径判一致",
                        }
                    )

            # --- 空值调和：v1 在比对阶段把显式空值视为缺失维度 ---
            missing = list(missing_finding.impls) if missing_finding else []
            if null_finding is not None:
                missing.extend(null_finding.impls)
                differences.append(
                    {
                        "type": "null_value",
                        "dimension": dimension,
                        "impls": list(null_finding.impls),
                        "v1_handling": "treated_as_missing",
                        "v2_handling": "null_value_finding",
                        "resolution": "kept_v1",
                        "note": "v2 单独报告空值；v1 比对阶段将其视为缺失维度，"
                        "兼容层按 v1 口径归并",
                    }
                )

            if real_gap:
                value_mismatches += 1
                detail = "数值超出容差"
                if missing:
                    detail += f"；同时缺少实现: {', '.join(missing)}"
                diffs.append(
                    DimensionDiff(
                        dimension=dimension,
                        kind="value_mismatch",
                        values=aligned,
                        max_delta=gap.margin if gap else None,
                        detail=detail,
                    )
                )
            elif missing:
                missing_dimensions += 1
                diffs.append(
                    DimensionDiff(
                        dimension=dimension,
                        kind="missing_dimension",
                        values=aligned,
                        max_delta=None,
                        detail=f"以下实现缺少该维度: {', '.join(missing)}",
                    )
                )

        if value_mismatches:
            verdict = "inconsistent"
        elif missing_dimensions:
            verdict = "partial"
        else:
            verdict = "consistent"
        summary = {
            "impl_count": len(impl_ids),
            "dimension_count": len(report.slices),
            "consistent_dimensions": len(report.slices) - len(diffs),
            "value_mismatches": value_mismatches,
            "missing_dimensions": missing_dimensions,
            "diff_count": len(diffs),
        }
        result = ComparisonResult(
            metric_key=metric_key,
            metric_version=int(metric_version),
            impl_ids=impl_ids,
            verdict=verdict,
            dimensions=list(report.slices),
            diffs=diffs,
            summary=summary,
            abs_tolerance=float(abs_tolerance),
            rel_tolerance=float(rel_tolerance),
        )
        return result, differences
