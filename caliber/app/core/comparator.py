"""比对调度：执行主干只负责编排，空值/阈值语义全部委托给质量语义提供者。

历史背景：本模块曾经内联实现容差比较与空值处理（即 dqkit 1.x 语义）。
质量库升级到 dqkit 2.x 后，这些语义迁移到了 ``app.core.quality`` 的
可插拔提供者（legacy / compat / v2），本模块不再硬编码任何旧语义。
"""
from __future__ import annotations

from typing import Mapping

from ..errors import CaliberError
from ..models import ComparisonResult
from .quality import get_semantics
from .quality.base import QualitySemantics


def _resolve(semantics: QualitySemantics | None) -> QualitySemantics:
    return semantics if semantics is not None else get_semantics(None)


def values_match(
    left: float,
    right: float,
    abs_tolerance: float = 1e-6,
    rel_tolerance: float = 0.0,
    *,
    semantics: QualitySemantics | None = None,
) -> bool:
    """按当前语义模式判断两个数值是否落在容差内。"""
    return _resolve(semantics).pair_match(left, right, abs_tolerance, rel_tolerance)


def compare_impl_results(
    results: Mapping[str, Mapping[str, float | None]],
    *,
    metric_key: str = "metric",
    metric_version: int = 0,
    abs_tolerance: float = 1e-6,
    rel_tolerance: float = 0.0,
    semantics: QualitySemantics | None = None,
) -> ComparisonResult:
    """对多实现结果做一致性比对，并把执行元信息附加到结果上。"""
    if abs_tolerance < 0 or rel_tolerance < 0:
        raise CaliberError("容差必须是非负数")
    sem = _resolve(semantics)
    comparison, differences = sem.compare_impls(
        results,
        metric_key=metric_key,
        metric_version=metric_version,
        abs_tolerance=abs_tolerance,
        rel_tolerance=rel_tolerance,
    )
    comparison.execution_meta = {
        "semantics_mode": sem.mode,
        "engine": sem.engine_meta(),
        "semantics_applied": sem.semantics_applied(),
        "semantic_differences": differences,
    }
    return comparison
