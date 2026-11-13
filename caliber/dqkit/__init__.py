"""dqkit — 外部数据质量库（本仓库内 vendored 锁定，当前为 2.0.0）。

caliber 的规则执行链路通过 ``app.core.quality`` 的语义提供者访问本库，
不直接依赖任何具体版本接口。本包暴露的是 **2.x 新接口**：

* 入参形态：规格对象（``Threshold`` / ``NullPolicy`` / ``CheckRequest``），
  取代 1.x 的位置参数标量。
* 返回形态：结构化 ``CheckReport``（``outcome`` / ``findings`` / ``stats``），
  取代 1.x 的松散字典（``verdict`` / ``diffs`` / ``summary``）。
* 语义变化（相对 1.x 的破坏性变更，详见 ``dqkit._v2`` 文档字符串）：
  空值不再报错而是标记后继续；阈值区间由闭区间改为开区间。

``dqkit.v1`` 是冻结的 1.4.2 旧版拷贝，仅用于迁移基线对照与回退，
执行主干不得直接引用。
"""
from __future__ import annotations

from ._v2 import (
    API_VERSION,
    CheckReport,
    CheckRequest,
    DqkitError,
    DqkitTypeError,
    Finding,
    NullPolicy,
    PairOutcome,
    Threshold,
    ValueAssessment,
    __version__,
    assess_value,
    check,
    compare_pair,
)

__all__ = [
    "API_VERSION",
    "CheckReport",
    "CheckRequest",
    "DqkitError",
    "DqkitTypeError",
    "Finding",
    "NullPolicy",
    "PairOutcome",
    "Threshold",
    "ValueAssessment",
    "__version__",
    "assess_value",
    "check",
    "compare_pair",
]
