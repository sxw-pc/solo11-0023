"""Pure multi-implementation comparison and verdict calculation."""
from __future__ import annotations

from itertools import combinations
from typing import Mapping

from ..errors import CaliberError
from ..models import ComparisonResult, DimensionDiff


def values_match(
    left: float,
    right: float,
    abs_tolerance: float = 1e-6,
    rel_tolerance: float = 0.0,
) -> bool:
    """独立的绝对/相对容差判定：任一满足即视为落在容差内，边界相等算通过。

    两个容差彼此独立，不做加和：
    ``|left-right| <= abs_tolerance`` 或
    ``|left-right| <= rel_tolerance * max(|left|, |right|)``。
    相对容差以两个数值中绝对值较大者为基数。
    """
    difference = abs(left - right)
    if difference <= abs_tolerance:
        return True
    scale = max(abs(left), abs(right))
    return scale > 0 and difference <= rel_tolerance * scale


def compare_impl_results(
    results: Mapping[str, Mapping[str, float]],
    *,
    metric_key: str = "metric",
    metric_version: int = 0,
    abs_tolerance: float = 1e-6,
    rel_tolerance: float = 0.0,
) -> ComparisonResult:
    if abs_tolerance < 0 or rel_tolerance < 0:
        raise CaliberError("容差必须是非负数")
    impl_ids = list(results)
    dimensions = sorted({dimension for values in results.values() for dimension in values})
    diffs: list[DimensionDiff] = []
    value_mismatches = 0
    missing_dimensions = 0

    for dimension in dimensions:
        aligned: dict[str, float | None] = {
            impl_id: results[impl_id].get(dimension) for impl_id in impl_ids
        }
        present = {
            impl_id: float(value)
            for impl_id, value in aligned.items()
            if value is not None
        }
        missing = [impl_id for impl_id, value in aligned.items() if value is None]
        max_delta = 0.0
        mismatch = False
        for left_id, right_id in combinations(present, 2):
            delta = abs(present[left_id] - present[right_id])
            max_delta = max(max_delta, delta)
            if not values_match(
                present[left_id], present[right_id], abs_tolerance, rel_tolerance
            ):
                mismatch = True
        if mismatch:
            value_mismatches += 1
            detail = "数值超出容差"
            if missing:
                detail += f"；同时缺少实现: {', '.join(missing)}"
            diffs.append(
                DimensionDiff(
                    dimension=dimension,
                    kind="value_mismatch",
                    values=aligned,
                    max_delta=max_delta,
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
        "dimension_count": len(dimensions),
        "consistent_dimensions": len(dimensions) - len(diffs),
        "value_mismatches": value_mismatches,
        "missing_dimensions": missing_dimensions,
        "diff_count": len(diffs),
    }
    return ComparisonResult(
        metric_key=metric_key,
        metric_version=int(metric_version),
        impl_ids=impl_ids,
        verdict=verdict,
        dimensions=dimensions,
        diffs=diffs,
        summary=summary,
        abs_tolerance=float(abs_tolerance),
        rel_tolerance=float(rel_tolerance),
    )
