"""dqkit 2.x 引擎实现。

相对 1.x 的破坏性变更（升级迁移时必须显式处理的语义差异）：

1. **空值判定**：1.x 遇到 NULL 指标值直接抛 ``NullValueError``；2.x 移除
   了"空值即错误"的策略，``assess_value`` 对空值返回带 ``"null"`` 标记的
   ``ValueAssessment``，``check`` 将其汇总为 ``null_value`` 发现项并计入
   统计，不再中断求值。``NullPolicy`` 只保留 ``"flag"`` 一种取值，传入
   1.x 的 ``"error"`` 会立即报错。
2. **阈值区间**：1.x 为闭区间 ``|a-b| <= abs + rel*max(|a|,|b|)``（边界
   视为一致）；2.x 改为**开区间** ``|a-b| < abs + rel*max(|a|,|b|)``，
   差值恰好等于阈值时判定为不一致。``PairOutcome.boundary_hit`` 精确标出
   两版结论相反的点，供兼容适配层调和，避免静默漂移。
3. **接口形态**：位置参数标量 → 规格对象；松散字典 → 结构化报告
   （``verdict``→``outcome``、``diffs``→``findings``、``summary``→``stats``，
   字段名同步更名）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations
from typing import Any, Mapping

__version__ = "2.0.0"
API_VERSION = 2


class DqkitError(Exception):
    """dqkit 2.x 基础错误。"""


class DqkitTypeError(DqkitError):
    """指标值类型违规（非数值；空值不在此列，见 NullPolicy）。"""


@dataclass(frozen=True, slots=True)
class Threshold:
    """容差阈值规格。2.x 只支持开区间（``boundary="open"``）。"""

    absolute: float = 1e-6
    relative: float = 0.0
    boundary: str = "open"

    def __post_init__(self) -> None:
        if self.boundary != "open":
            raise DqkitError(
                f"dqkit 2.x 已移除闭区间阈值（boundary={self.boundary!r}），"
                "仅支持 'open'"
            )
        if self.absolute < 0 or self.relative < 0:
            raise DqkitError("threshold 的 absolute/relative 必须是非负数")


@dataclass(frozen=True, slots=True)
class NullPolicy:
    """空值策略。2.x 只保留 ``"flag"``（标记后继续）。"""

    on_null: str = "flag"

    def __post_init__(self) -> None:
        if self.on_null != "flag":
            raise DqkitError(
                f"dqkit 2.x 已移除空值策略 {self.on_null!r}（含 1.x 的 'error'），"
                "仅支持 'flag'"
            )


@dataclass(frozen=True, slots=True)
class ValueAssessment:
    """单个指标值的评估结果。"""

    value: float | None
    flags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PairOutcome:
    """两个实现同一维度数值的成对比较结果。"""

    left: str
    right: str
    delta: float
    threshold: float
    match: bool
    boundary_hit: bool  # delta == threshold：1.x 判一致、2.x 判不一致的分歧点


@dataclass(frozen=True, slots=True)
class Finding:
    """单个维度上的一类检查发现。"""

    type: str  # "value_gap" | "null_value" | "missing_slice"
    slice: str
    values: Mapping[str, float | None]  # 全部实现对齐后的视图（缺失/空值为 None）
    impls: tuple[str, ...] = ()  # 该发现涉及的实现（missing/null）；value_gap 为空
    margin: float | None = None  # value_gap：全部在场数值对的最大差值
    pairs: tuple[PairOutcome, ...] = ()  # value_gap：全部在场数值对的比较明细
    note: str = ""


@dataclass(frozen=True, slots=True)
class CheckRequest:
    """一次一致性检查的完整入参。"""

    impl_values: Mapping[str, Mapping[str, float | None]]
    threshold: Threshold = Threshold()
    null_policy: NullPolicy = NullPolicy()


@dataclass(frozen=True, slots=True)
class CheckReport:
    """结构化检查报告（2.x 返回形态）。"""

    outcome: str  # "pass" | "attention" | "fail"
    slices: tuple[str, ...]
    findings: tuple[Finding, ...]
    stats: Mapping[str, Any]
    engine: Mapping[str, Any] = field(default_factory=dict)


def assess_value(raw: Any, null_policy: NullPolicy | None = None) -> ValueAssessment:
    """评估单个指标值。空值按策略标记，类型违规仍然报错。"""
    policy = null_policy or NullPolicy()
    if raw is None:
        return ValueAssessment(None, ("null",) if policy.on_null == "flag" else ())
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise DqkitTypeError(f"指标值不是数值: {raw!r}")
    return ValueAssessment(float(raw), ())


def compare_pair(
    left: float,
    right: float,
    threshold: Threshold,
    *,
    left_id: str = "",
    right_id: str = "",
) -> PairOutcome:
    """开区间成对比较：``|a-b| < abs + rel*max(|a|,|b|)`` 才算一致。"""
    delta = abs(left - right)
    scale = max(abs(left), abs(right))
    limit = threshold.absolute + threshold.relative * scale
    boundary_hit = delta == limit
    return PairOutcome(
        left=left_id,
        right=right_id,
        delta=delta,
        threshold=limit,
        match=delta < limit,
        boundary_hit=boundary_hit,
    )


def check(request: CheckRequest) -> CheckReport:
    """对多实现的 ``维度 -> 数值`` 结果执行一致性检查。"""
    threshold = request.threshold
    impl_ids = list(request.impl_values)
    slices = sorted({s for values in request.impl_values.values() for s in values})
    findings: list[Finding] = []
    value_gaps = 0
    missing_slices = 0
    null_values = 0

    for slice_ in slices:
        aligned: dict[str, float | None] = {}
        present: dict[str, float] = {}
        missing: list[str] = []
        nulls: list[str] = []
        for impl_id in impl_ids:
            values = request.impl_values[impl_id]
            if slice_ not in values:
                aligned[impl_id] = None
                missing.append(impl_id)
                continue
            raw = values[slice_]
            if raw is None:
                aligned[impl_id] = None
                nulls.append(impl_id)
                continue
            aligned[impl_id] = float(raw)
            present[impl_id] = float(raw)

        pairs: list[PairOutcome] = []
        max_delta = 0.0
        gap = False
        for left_id, right_id in combinations(present, 2):
            outcome = compare_pair(
                present[left_id],
                present[right_id],
                threshold,
                left_id=left_id,
                right_id=right_id,
            )
            pairs.append(outcome)
            max_delta = max(max_delta, outcome.delta)
            if not outcome.match:
                gap = True
        if gap:
            value_gaps += 1
            findings.append(
                Finding(
                    type="value_gap",
                    slice=slice_,
                    values=dict(aligned),
                    margin=max_delta,
                    pairs=tuple(pairs),
                    note="数值差超出开区间阈值",
                )
            )
        if nulls:
            null_values += len(nulls)
            findings.append(
                Finding(
                    type="null_value",
                    slice=slice_,
                    values=dict(aligned),
                    impls=tuple(nulls),
                    note="指标值为空（2.x 标记后继续，1.x 会直接报错）",
                )
            )
        if missing:
            missing_slices += 1
            findings.append(
                Finding(
                    type="missing_slice",
                    slice=slice_,
                    values=dict(aligned),
                    impls=tuple(missing),
                    note="实现缺少该维度",
                )
            )

    if value_gaps:
        outcome = "fail"
    elif missing_slices or null_values:
        outcome = "attention"
    else:
        outcome = "pass"
    affected = {finding.slice for finding in findings}
    stats: dict[str, Any] = {
        "impl_count": len(impl_ids),
        "slices_total": len(slices),
        "slices_clean": len(slices) - len(affected),
        "value_gaps": value_gaps,
        "missing_slices": missing_slices,
        "null_values": null_values,
        "findings": len(findings),
    }
    return CheckReport(
        outcome=outcome,
        slices=tuple(slices),
        findings=tuple(findings),
        stats=stats,
        engine={"name": "dqkit", "version": __version__, "api": API_VERSION},
    )
