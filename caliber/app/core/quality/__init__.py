"""质量语义提供者：执行主干与数据质量库（dqkit）之间的接缝。

升级背景：底层质量库已从 dqkit 1.4.2 切到 dqkit 2.0.0（新大版本）。
2.x 在**空值判定**（报错 → 标记后继续）和**阈值区间**（闭区间 → 开区间）
两处语义上发生了变化，接口的入参形态与返回字段也已重排。

执行主干（evaluator / comparator / snapshot）不硬编码任何空值或阈值
策略，而是委托给可插拔的语义提供者：

* ``legacy`` —— 冻结的 dqkit 1.4.2 引擎，**回退路径**；
* ``compat`` —— **兼容适配层（默认）**：驱动 dqkit 2.x 执行，把每个结果
  调和回 1.x 基线口径，并把每一处语义差异显式记录到执行元信息；
* ``v2`` —— 纯 dqkit 2.x 新语义（关闭兼容层后的目标形态）。

切换方式：CLI ``--quality-mode``、API 请求体 ``quality_mode``、环境变量
``CALIBER_QUALITY_MODE``，或 ``Config.quality_mode``。
"""
from __future__ import annotations

import os
from typing import Any

import dqkit

from ...errors import CaliberError, QualityVersionMismatch

MODE_LEGACY = "legacy"
MODE_COMPAT = "compat"
MODE_V2 = "v2"
MODES = (MODE_LEGACY, MODE_COMPAT, MODE_V2)
DEFAULT_MODE = MODE_COMPAT
ENV_VAR = "CALIBER_QUALITY_MODE"

#: 当前适配层支持的 dqkit 主版本 API。
SUPPORTED_ENGINE_API = 2


def check_engine_version() -> None:
    """校验已安装 dqkit 的 API 版本，不匹配时给出可读错误而非崩栈。"""
    api = getattr(dqkit, "API_VERSION", None)
    version = getattr(dqkit, "__version__", "未知")
    if api != SUPPORTED_ENGINE_API:
        raise QualityVersionMismatch(
            f"数据质量库版本不匹配: caliber 适配层支持 dqkit API v{SUPPORTED_ENGINE_API}，"
            f"当前安装 dqkit {version}（API v{api}）。"
            "请将 dqkit 回退到 2.x，或升级 caliber 的兼容适配层后再执行。",
            details={
                "expected_api": SUPPORTED_ENGINE_API,
                "installed_version": version,
                "installed_api": api,
            },
        )


def engine_meta() -> dict[str, Any]:
    return {"name": "dqkit", "version": dqkit.__version__, "api": dqkit.API_VERSION}


def legacy_artifact_meta() -> dict[str, Any]:
    """迁移前产物的执行元信息（读取老快照时回填，显式标记而非静默缺省）。"""
    from dqkit import v1

    return {
        "semantics_mode": MODE_LEGACY,
        "engine": {"name": "dqkit", "version": v1.__version__, "api": v1.API_VERSION},
        "semantics_applied": {
            "null_handling": "error_on_null (dqkit v1)",
            "threshold_boundary": "inclusive (dqkit v1)",
        },
        "semantic_differences": [],
        "artifact_note": "迁移前产物：执行元信息在读取时按 v1 口径回填",
    }


def get_semantics(mode: str | None = None):
    """按模式构造语义提供者；未知模式与版本不匹配都给出可读错误。"""
    resolved = (mode or os.environ.get(ENV_VAR) or DEFAULT_MODE).strip().lower()
    if resolved not in MODES:
        raise CaliberError(
            f"未知质量语义模式: {resolved!r}；可选值: {', '.join(MODES)}",
            code="unknown_quality_mode",
        )
    if resolved == MODE_LEGACY:
        from .legacy import LegacySemantics

        return LegacySemantics()
    if resolved == MODE_V2:
        from .v2 import V2Semantics

        return V2Semantics()
    from .compat import CompatSemantics

    return CompatSemantics()
