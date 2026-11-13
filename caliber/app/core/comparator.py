"""Pure multi-implementation comparison and verdict calculation."""
from __future__ import annotations

from itertools import combinations
from typing import Any, Mapping

from ..errors import CaliberError
from ..models import ComparisonResult, DimensionDiff

# 执行元信息：粒度轴来源与设计取舍说明，随 summary_json/result_json 持久化。
AXES_SOURCE = "dsl_declaration"
AXES_RATIONALE = (
    "粒度轴取自指标 YAML 的 grain_axes 显式声明，而非 SQL 结果列名推断："
    "声明对全部实现施加同一套轴，判定确定且可复现；"
    "列名推断依赖各实现的别名习惯，容易误判，且两个实现推断出的轴集合可能不一致。"
)
LEGACY_MODE_NOTE = (
    "该指标未声明 grain_axes，沿用历史总量/维度比对（legacy_total），行为与旧版完全一致；"
    "这也是轴信息缺失（如推断失准）时的受控退化路径。"
)
UNALIGNED_NOTE = (
    "已声明 grain_axes，但至少一个实现的 SQL 结果缺少声明的粒度列，"
    "按约定标记为不可对齐（unaligned）并定位缺失列，不静默退回总量比对。"
)


def _legacy_grain_check() -> dict[str, Any]:
    return {
        "mode": "legacy_total",
        "declared_axes": [],
        "status": "not_declared",
        "axes_source": None,
        "missing_columns": {},
        "note": LEGACY_MODE_NOTE,
    }


def values_match(
    left: float,
    right: float,
    abs_tolerance: float = 1e-6,
    rel_tolerance: float = 0.0,
) -> bool:
    """Absolute + relative tolerance; equality at the boundary is a match."""
    difference = abs(left - right)
    scale = max(abs(left), abs(right))
    return difference <= abs_tolerance + rel_tolerance * scale


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
        "grain_check": _legacy_grain_check(),
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


def _unaligned_result(
    results: Mapping[str, Mapping[str, float]],
    *,
    axes: list[str],
    impl_columns: Mapping[str, list[str]],
    missing_axes: Mapping[str, list[str]],
    metric_key: str,
    metric_version: int,
    abs_tolerance: float,
    rel_tolerance: float,
) -> ComparisonResult:
    """Declared axes cannot be honored: mark unaligned, never fall back to totals."""
    missing_axes = {impl: list(cols) for impl, cols in sorted(missing_axes.items())}
    detail = "；".join(
        f"{impl} 缺少粒度列: {', '.join(cols)}" for impl, cols in missing_axes.items()
    )
    grain_alignment = {
        "status": "unaligned",
        "axes": list(axes),
        "impl_columns": {impl: list(cols) for impl, cols in impl_columns.items()},
        "missing_columns": missing_axes,
        "groups": [],
        "group_counts": {"total": 0, "consistent": 0, "mismatch": 0, "missing": 0},
        "detail": detail,
        "meta": {
            "axes_source": AXES_SOURCE,
            "rationale": AXES_RATIONALE,
            "fallback": UNALIGNED_NOTE,
        },
    }
    summary = {
        "impl_count": len(results),
        "dimension_count": 0,
        "consistent_dimensions": 0,
        "value_mismatches": 0,
        "missing_dimensions": 0,
        "diff_count": 0,
        "grain_check": {
            "mode": "grain_axes",
            "declared_axes": list(axes),
            "status": "unaligned",
            "axes_source": AXES_SOURCE,
            "missing_columns": missing_axes,
            "note": UNALIGNED_NOTE,
        },
        "group_counts": grain_alignment["group_counts"],
    }
    return ComparisonResult(
        metric_key=metric_key,
        metric_version=int(metric_version),
        impl_ids=list(results),
        verdict="inconsistent",
        dimensions=[],
        diffs=[],
        summary=summary,
        abs_tolerance=float(abs_tolerance),
        rel_tolerance=float(rel_tolerance),
        grain_alignment=grain_alignment,
    )


def compare_grain_results(
    results: Mapping[str, Mapping[str, float]],
    *,
    axis_map: Mapping[str, Mapping[str, str]],
    axes: list[str],
    impl_columns: Mapping[str, list[str]],
    missing_axes: Mapping[str, list[str]],
    metric_key: str = "metric",
    metric_version: int = 0,
    abs_tolerance: float = 1e-6,
    rel_tolerance: float = 0.0,
) -> ComparisonResult:
    """Align implementations on declared grain-axis groups, then compare per group.

    ``results`` maps impl_id -> {group_label: value}; ``axis_map`` maps each
    group label back to its {axis: value} coordinates.  Per-group semantics
    reuse the legacy comparator exactly: value pairs use the same tolerance
    formula, and a group present on only one side is deterministically
    classified ``missing`` (contributing to a ``partial`` verdict), never random.
    """
    if abs_tolerance < 0 or rel_tolerance < 0:
        raise CaliberError("容差必须是非负数")
    axes = list(axes)
    missing_axes = {impl: list(cols) for impl, cols in missing_axes.items() if cols}
    if missing_axes:
        return _unaligned_result(
            results,
            axes=axes,
            impl_columns=impl_columns,
            missing_axes=missing_axes,
            metric_key=metric_key,
            metric_version=metric_version,
            abs_tolerance=abs_tolerance,
            rel_tolerance=rel_tolerance,
        )
    base = compare_impl_results(
        results,
        metric_key=metric_key,
        metric_version=metric_version,
        abs_tolerance=abs_tolerance,
        rel_tolerance=rel_tolerance,
    )
    diff_by_label = {diff.dimension: diff for diff in base.diffs}
    groups: list[dict[str, Any]] = []
    counts = {"consistent": 0, "mismatch": 0, "missing": 0}
    for label in base.dimensions:
        diff = diff_by_label.get(label)
        if diff is None:
            status = "consistent"
        elif diff.kind == "value_mismatch":
            status = "mismatch"
        else:
            status = "missing"
        counts[status] += 1
        groups.append(
            {
                "key": dict(axis_map.get(label, {})),
                "label": label,
                "values": {impl_id: results[impl_id].get(label) for impl_id in base.impl_ids},
                "status": status,
                "max_delta": diff.max_delta if diff is not None else 0.0,
            }
        )
    group_counts = {"total": len(groups), **counts}
    base.grain_alignment = {
        "status": "aligned",
        "axes": axes,
        "impl_columns": {impl: list(cols) for impl, cols in impl_columns.items()},
        "missing_columns": {},
        "groups": groups,
        "group_counts": group_counts,
        "detail": "",
        "meta": {
            "axes_source": AXES_SOURCE,
            "rationale": AXES_RATIONALE,
            "fallback": LEGACY_MODE_NOTE,
        },
    }
    base.summary["grain_check"] = {
        "mode": "grain_axes",
        "declared_axes": axes,
        "status": "aligned",
        "axes_source": AXES_SOURCE,
        "missing_columns": {},
        "note": AXES_RATIONALE,
    }
    base.summary["group_counts"] = group_counts
    return base
