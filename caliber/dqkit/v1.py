"""dqkit 1.4.2 — 冻结的旧版接口（迁移基线 / 回退引擎）。

本模块是数据质量库 1.x 最后一个版本的归档拷贝。caliber 的规则执行链路
最初就是按这些接口编写的：位置参数标量入参、松散字典出参、空值即错误、
闭区间阈值。

保留它的唯一目的：

* 作为黄金主版本（golden master）对照——兼容性测试用它证明适配层在
  dqkit 2.x 上复现了 1.x 的判定结果；
* 作为显式回退路径——``CALIBER_QUALITY_MODE=legacy`` 时由
  ``app.core.quality.legacy`` 包装调用。

执行主干（``app.core.evaluator`` / ``app.core.comparator``）不得直接
导入本模块；新代码请使用 ``dqkit`` 顶层暴露的 2.x 接口。
"""
from __future__ import annotations

from itertools import combinations
from typing import Any, Mapping

__version__ = "1.4.2"
API_VERSION = 1


class DqkitError(Exception):
    """dqkit 1.x 基础错误。"""


class NullValueError(DqkitError):
    """指标值为 NULL（1.x 视为契约违规，直接报错）。"""


class TypeViolationError(DqkitError):
    """指标值不是数值。"""


def validate_number(value: Any) -> float:
    """1.x 空值判定：NULL 抛 NullValueError，非数值抛 TypeViolationError。"""
    if value is None:
        raise NullValueError("指标值为 NULL")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeViolationError(f"指标值不是数值: {value!r}")
    return float(value)


def within_tolerance(left: float, right: float, abs_tol: float, rel_tol: float) -> bool:
    """1.x 闭区间阈值：``|a-b| <= abs_tol + rel_tol * max(|a|, |b|)``。"""
    difference = abs(left - right)
    scale = max(abs(left), abs(right))
    return difference <= abs_tol + rel_tol * scale


def summarize(
    results: Mapping[str, Mapping[str, float]],
    *,
    abs_tolerance: float = 1e-6,
    rel_tolerance: float = 0.0,
) -> dict[str, Any]:
    """1.x 比对汇总，返回旧版字段形态的字典。

    返回 ``{"impl_ids", "verdict", "dimensions", "diffs", "summary"}``；
    ``diffs`` 的 kind 取值为旧版词汇 ``value_mismatch`` / ``missing_dimension``。
    """
    if abs_tolerance < 0 or rel_tolerance < 0:
        raise DqkitError("容差必须是非负数")
    impl_ids = list(results)
    dimensions = sorted({dimension for values in results.values() for dimension in values})
    diffs: list[dict[str, Any]] = []
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
            if not within_tolerance(
                present[left_id], present[right_id], abs_tolerance, rel_tolerance
            ):
                mismatch = True
        if mismatch:
            value_mismatches += 1
            detail = "数值超出容差"
            if missing:
                detail += f"；同时缺少实现: {', '.join(missing)}"
            diffs.append(
                {
                    "dimension": dimension,
                    "kind": "value_mismatch",
                    "values": aligned,
                    "max_delta": max_delta,
                    "detail": detail,
                }
            )
        elif missing:
            missing_dimensions += 1
            diffs.append(
                {
                    "dimension": dimension,
                    "kind": "missing_dimension",
                    "values": aligned,
                    "max_delta": None,
                    "detail": f"以下实现缺少该维度: {', '.join(missing)}",
                }
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
    return {
        "impl_ids": impl_ids,
        "verdict": verdict,
        "dimensions": dimensions,
        "diffs": diffs,
        "summary": summary,
    }
