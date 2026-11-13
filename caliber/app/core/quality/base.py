"""语义提供者的抽象基类：执行主干只依赖这个接口。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Mapping

from ...models import ComparisonResult


class QualitySemantics(ABC):
    """空值判定与阈值比较的语义来源（新旧可切换、可插拔）。"""

    mode: str = "abstract"

    @abstractmethod
    def engine_meta(self) -> dict[str, Any]:
        """底层质量引擎标识，写入执行元信息。"""

    @abstractmethod
    def semantics_applied(self) -> dict[str, str]:
        """当前生效的空值/阈值语义描述，写入执行元信息。"""

    @abstractmethod
    def assess_value(self, raw: Any, *, impl_id: str) -> float | None:
        """评估单个指标值；返回数值，或按语义携带空值（None）。"""

    @abstractmethod
    def pair_match(
        self, left: float, right: float, abs_tolerance: float, rel_tolerance: float
    ) -> bool:
        """两个数值是否落在容差内。"""

    @abstractmethod
    def compare_impls(
        self,
        results: Mapping[str, Mapping[str, float | None]],
        *,
        metric_key: str,
        metric_version: int,
        abs_tolerance: float,
        rel_tolerance: float,
    ) -> tuple[ComparisonResult, list[dict[str, Any]]]:
        """执行多实现比对，返回（比对结果，语义差异记录列表）。"""
