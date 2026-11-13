"""Baseline run alignment and batch-to-batch comparison.

策略（写入执行元信息 ``strategy`` 字段）：``pre_align_post_finalize``。

- run 创建、任何指标求值之前，先解析基线 run（显式指定或自动选取上一个
  同指标集合的 completed run），校验其状态，并把基线快照按 metric_key 与
  本次待跑指标清单做结构对齐，结果写入审计日志。
- 指标执行与快照落库路径完全不感知基线；全量跑完后，再基于两侧已经落库的
  不可变快照定稿对照结论，扩展写入 ``runs.summary_json``。
- 基线 run 失败/半成品、找不到候选基线、指标缺失、版本不一致、impl 集合
  变化都不会拒绝整批，而是逐指标标记不可比，或把整批降级为无对照并写明原因。

快照表结构保持不变，所有对照结论只落在 runs.summary_json 的扩展字段里，
老报告与老调用路径不受影响。
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from ..errors import NotFoundError
from ..models import utc_now
from .snapshot import snapshots_for_run
from .versioning import get_latest

STRATEGY = "pre_align_post_finalize"

VERDICT_RANK = {"consistent": 0, "partial": 1, "inconsistent": 2}

# 整批降级原因（本批次无有效对照）
REASON_NO_MATCHING_BASELINE = "no_matching_baseline"
REASON_BASELINE_NOT_FOUND = "baseline_run_not_found"
REASON_BASELINE_NOT_COMPLETED = "baseline_not_completed"
REASON_CURRENT_RUN_FAILED = "current_run_failed"
REASON_DISABLED = "comparison_disabled"

# 逐指标不可比原因
REASON_BASELINE_METRIC_MISSING = "baseline_metric_missing"
REASON_VERSION_MISMATCH = "metric_version_mismatch"
REASON_IMPL_SET_CHANGED = "impl_set_changed"

_DEGRADE_TEXT = {
    REASON_NO_MATCHING_BASELINE: "没有找到指标集合完全一致的已完成历史 run，无法自动选取基线",
    REASON_BASELINE_NOT_FOUND: "指定的基线 run 不存在",
    REASON_BASELINE_NOT_COMPLETED: "基线 run 不是 completed 状态（失败或半成品不能作为有效对照）",
    REASON_CURRENT_RUN_FAILED: "本次 run 执行失败，对照未完成",
    REASON_DISABLED: "已显式关闭基线对照",
}


@dataclass(slots=True)
class BaselinePlan:
    """run 开始前解析出的基线锚点与结构对齐结果。"""

    enabled: bool
    selection: str  # "explicit" | "auto" | "none"
    status: str  # "ready" | "degraded"
    anchored_at: str
    baseline_run_id: int | None = None
    degrade_reason: str | None = None
    degrade_detail: dict[str, Any] = field(default_factory=dict)
    # metric_key -> 基线解码快照（仅 status=ready 时填充）
    baseline_snapshots: dict[str, dict[str, Any]] = field(default_factory=dict)
    # metric_key -> 执行前结构对齐记录
    alignment: dict[str, dict[str, Any]] = field(default_factory=dict)

    def anchor(self) -> dict[str, Any]:
        """可序列化的锚点摘要，用于审计与失败 run 的 summary。"""
        return {
            "enabled": self.enabled,
            "strategy": STRATEGY,
            "status": self.status,
            "baseline_selection": self.selection,
            "baseline_run_id": self.baseline_run_id,
            "degrade_reason": self.degrade_reason,
            "degrade_detail": self.degrade_detail,
            "anchored_at": self.anchored_at,
        }


def _disabled_plan() -> BaselinePlan:
    return BaselinePlan(
        enabled=False,
        selection="none",
        status="degraded",
        degrade_reason=REASON_DISABLED,
        anchored_at=utc_now(),
    )


def _degraded_plan(
    reason: str,
    selection: str,
    *,
    baseline_run_id: int | None = None,
    detail: dict[str, Any] | None = None,
) -> BaselinePlan:
    return BaselinePlan(
        enabled=True,
        selection=selection,
        status="degraded",
        degrade_reason=reason,
        degrade_detail=detail or {},
        baseline_run_id=baseline_run_id,
        anchored_at=utc_now(),
    )


def _auto_pick_baseline(conn: sqlite3.Connection, keys: list[str]) -> int | None:
    """选取最近一个 status=completed 且指标集合与本次完全相同的历史 run。"""
    target = set(keys)
    rows = conn.execute(
        "SELECT id, metrics_json FROM runs WHERE status = 'completed' "
        "ORDER BY id DESC LIMIT 50"
    ).fetchall()
    for row in rows:
        try:
            historical = json.loads(row["metrics_json"])
        except json.JSONDecodeError:
            continue
        if isinstance(historical, list) and set(map(str, historical)) == target:
            return int(row["id"])
    return None


def _structural_reasons(
    key: str,
    baseline_snapshot: dict[str, Any] | None,
    current_version: int,
    current_impl_ids: list[str],
) -> tuple[list[str], dict[str, Any]]:
    """执行前的结构对齐：缺失 / 版本 / impl 集合三类不可比判定。"""
    reasons: list[str] = []
    detail: dict[str, Any] = {
        "current_version": current_version,
        "current_impl_ids": list(current_impl_ids),
    }
    if baseline_snapshot is None:
        reasons.append(REASON_BASELINE_METRIC_MISSING)
        return reasons, detail
    baseline_version = int(baseline_snapshot["metric_version"])
    baseline_impl_ids = list(baseline_snapshot["impl_ids"])
    detail.update(
        baseline_version=baseline_version,
        baseline_impl_ids=baseline_impl_ids,
        baseline_verdict=baseline_snapshot["verdict"],
    )
    if baseline_version != current_version:
        reasons.append(REASON_VERSION_MISMATCH)
    baseline_set, current_set = set(baseline_impl_ids), set(current_impl_ids)
    if baseline_set != current_set:
        reasons.append(REASON_IMPL_SET_CHANGED)
        detail["impls_added"] = sorted(current_set - baseline_set)
        detail["impls_removed"] = sorted(baseline_set - current_set)
    return reasons, detail


def resolve_baseline(
    conn: sqlite3.Connection,
    keys: list[str],
    *,
    baseline_run_id: int | None = None,
    compare_baseline: bool = True,
) -> BaselinePlan:
    """在 run 创建与求值之前解析基线并完成结构对齐（只读，不写任何业务表）。"""
    if not compare_baseline:
        return _disabled_plan()

    selected_id: int | None
    selection: str
    if baseline_run_id is not None:
        selected_id = int(baseline_run_id)
        selection = "explicit"
        row = conn.execute(
            "SELECT id, status FROM runs WHERE id = ?", (selected_id,)
        ).fetchone()
        if row is None:
            return _degraded_plan(
                REASON_BASELINE_NOT_FOUND,
                selection,
                baseline_run_id=selected_id,
            )
        if row["status"] != "completed":
            return _degraded_plan(
                REASON_BASELINE_NOT_COMPLETED,
                selection,
                baseline_run_id=selected_id,
                detail={"baseline_status": row["status"]},
            )
    else:
        selection = "auto"
        selected_id = _auto_pick_baseline(conn, keys)
        if selected_id is None:
            return _degraded_plan(REASON_NO_MATCHING_BASELINE, selection)

    baseline_rows = snapshots_for_run(conn, selected_id)
    baseline_snapshots = {item["metric_key"]: item for item in baseline_rows}

    alignment: dict[str, dict[str, Any]] = {}
    for key in keys:
        try:
            latest = get_latest(conn, key)
        except NotFoundError:
            # 预检不对缺失指标下结论；规范的 NotFound 错误留给执行循环抛出，
            # 以保持失败 run 落库与 run.failed 审计的既有语义。
            continue
        current_version = int(latest["version"])
        current_impl_ids = [
            str(item.get("id", ""))
            for item in latest["definition"].get("impls", [])
            if isinstance(item, dict)
        ]
        baseline_snapshot = baseline_snapshots.get(key)
        reasons, detail = _structural_reasons(
            key, baseline_snapshot, current_version, current_impl_ids
        )
        alignment[key] = {
            "comparable": not reasons,
            "reasons": reasons,
            **detail,
        }

    return BaselinePlan(
        enabled=True,
        selection=selection,
        status="ready",
        baseline_run_id=selected_id,
        baseline_snapshots=baseline_snapshots,
        alignment=alignment,
        anchored_at=utc_now(),
    )


def _diff_dimension_map(snapshot: dict[str, Any]) -> dict[str, str]:
    return {
        str(diff["dimension"]): str(diff["kind"])
        for diff in snapshot["result"].get("diffs", [])
    }


def _verdict_change(baseline_verdict: str, current_verdict: str) -> str:
    before = VERDICT_RANK.get(baseline_verdict, -1)
    after = VERDICT_RANK.get(current_verdict, -1)
    if after > before:
        return "regressed"
    if after < before:
        return "improved"
    return "unchanged"


def _incomparable_block(
    key: str,
    pre_aligned: dict[str, Any],
    baseline_snapshot: dict[str, Any] | None,
    current_snapshot: dict[str, Any],
) -> dict[str, Any]:
    reasons = list(pre_aligned.get("reasons", []))
    block: dict[str, Any] = {
        "metric_key": key,
        "comparable": False,
        "reasons": reasons,
        "current_version": int(current_snapshot["metric_version"]),
        "current_impl_ids": list(current_snapshot["impl_ids"]),
        "current_verdict": current_snapshot["verdict"],
    }
    if baseline_snapshot is not None:
        block.update(
            baseline_snapshot_id=baseline_snapshot["id"],
            baseline_version=int(baseline_snapshot["metric_version"]),
            baseline_impl_ids=list(baseline_snapshot["impl_ids"]),
            baseline_verdict=baseline_snapshot["verdict"],
        )
        baseline_set = set(block["baseline_impl_ids"])
        current_set = set(block["current_impl_ids"])
        block["impls_added"] = sorted(current_set - baseline_set)
        block["impls_removed"] = sorted(baseline_set - current_set)
    return block


def _comparable_block(
    key: str,
    baseline_snapshot: dict[str, Any],
    current_snapshot: dict[str, Any],
) -> dict[str, Any]:
    baseline_dims = list(baseline_snapshot["result"].get("dimensions", []))
    current_dims = list(current_snapshot["result"].get("dimensions", []))
    baseline_diff_map = _diff_dimension_map(baseline_snapshot)
    current_diff_map = _diff_dimension_map(current_snapshot)
    kind_changed = sorted(
        dim
        for dim in set(baseline_diff_map) & set(current_diff_map)
        if baseline_diff_map[dim] != current_diff_map[dim]
    )
    return {
        "metric_key": key,
        "comparable": True,
        "reasons": [],
        "baseline_snapshot_id": baseline_snapshot["id"],
        "baseline_version": int(baseline_snapshot["metric_version"]),
        "current_version": int(current_snapshot["metric_version"]),
        "baseline_impl_ids": list(baseline_snapshot["impl_ids"]),
        "current_impl_ids": list(current_snapshot["impl_ids"]),
        "baseline_verdict": baseline_snapshot["verdict"],
        "current_verdict": current_snapshot["verdict"],
        "verdict_change": _verdict_change(
            baseline_snapshot["verdict"], current_snapshot["verdict"]
        ),
        "dimension_changes": {
            "dimensions_added": sorted(set(current_dims) - set(baseline_dims)),
            "dimensions_removed": sorted(set(baseline_dims) - set(current_dims)),
            "diff_dimensions_added": sorted(
                set(current_diff_map) - set(baseline_diff_map)
            ),
            "diff_dimensions_removed": sorted(
                set(baseline_diff_map) - set(current_diff_map)
            ),
            "diff_kind_changed": kind_changed,
        },
    }


def _reason_text(key: str, block: dict[str, Any]) -> str:
    messages = []
    for reason in block["reasons"]:
        if reason == REASON_BASELINE_METRIC_MISSING:
            messages.append(f"{key}: 基线缺少该指标")
        elif reason == REASON_VERSION_MISMATCH:
            messages.append(
                f"{key}: 指标版本不一致（基线 v{block.get('baseline_version')} / "
                f"本次 v{block.get('current_version')}），不可静默对齐"
            )
        elif reason == REASON_IMPL_SET_CHANGED:
            messages.append(
                f"{key}: 实现集合变化（基线 {block.get('baseline_impl_ids')} / "
                f"本次 {block.get('current_impl_ids')}；新增 {block.get('impls_added')} / "
                f"移除 {block.get('impls_removed')}），不可比"
            )
    return "；".join(messages)


def _summary_text(
    plan: BaselinePlan,
    metrics: dict[str, dict[str, Any]],
    *,
    degraded: bool,
    degrade_reason: str | None = None,
) -> str:
    if degraded:
        return f"基线对照已降级：{_DEGRADE_TEXT.get(degrade_reason or '', degrade_reason)}"
    comparable = [item for item in metrics.values() if item["comparable"]]
    incomparable = [item for item in metrics.values() if not item["comparable"]]
    regressed = sum(1 for item in comparable if item["verdict_change"] == "regressed")
    improved = sum(1 for item in comparable if item["verdict_change"] == "improved")
    unchanged = sum(1 for item in comparable if item["verdict_change"] == "unchanged")
    text = (
        f"基线 run #{plan.baseline_run_id}（{plan.selection}）：可比指标 {len(comparable)} 个"
        f"（退化 {regressed} / 改善 {improved} / 持平 {unchanged}），"
        f"不可比指标 {len(incomparable)} 个"
    )
    if incomparable:
        detail = "；".join(_reason_text(key, block) for key, block in sorted(metrics.items())
                           if not block["comparable"])
        text += f"。不可比明细：{detail}"
    return text


def build_baseline_comparison(
    conn: sqlite3.Connection,
    plan: BaselinePlan,
    current_run_id: int,
) -> dict[str, Any]:
    """全量跑完后定稿：基于两侧不可变快照生成 summary_json 扩展块。"""
    base = {
        "enabled": True,
        "strategy": STRATEGY,
        "baseline_selection": plan.selection,
        "baseline_run_id": plan.baseline_run_id,
        "anchored_at": plan.anchored_at,
    }
    if plan.status == "degraded":
        return {
            **base,
            "status": "degraded",
            "degrade_reason": plan.degrade_reason,
            "degrade_detail": plan.degrade_detail,
            "comparable_count": 0,
            "incomparable_count": 0,
            "metrics": {},
            "summary_text": _summary_text(plan, {}, degraded=True,
                                          degrade_reason=plan.degrade_reason),
        }

    current_snapshots = {
        item["metric_key"]: item for item in snapshots_for_run(conn, current_run_id)
    }
    metrics: dict[str, dict[str, Any]] = {}
    for key in sorted(current_snapshots):
        current_snapshot = current_snapshots[key]
        baseline_snapshot = plan.baseline_snapshots.get(key)
        pre_aligned = plan.alignment.get(key, {})
        if baseline_snapshot is None or pre_aligned.get("reasons"):
            metrics[key] = _incomparable_block(
                key, pre_aligned, baseline_snapshot, current_snapshot
            )
        else:
            metrics[key] = _comparable_block(key, baseline_snapshot, current_snapshot)

    comparable_count = sum(1 for item in metrics.values() if item["comparable"])
    return {
        **base,
        "status": "ok",
        "degrade_reason": None,
        "degrade_detail": {},
        "comparable_count": comparable_count,
        "incomparable_count": len(metrics) - comparable_count,
        "metrics": metrics,
        "summary_text": _summary_text(plan, metrics, degraded=False),
    }


def failure_anchor(plan: BaselinePlan) -> dict[str, Any]:
    """当前 run 失败时写入失败 summary 的降级锚点（不含逐指标结论）。"""
    return {
        "enabled": plan.enabled,
        "strategy": STRATEGY,
        "status": "degraded",
        "baseline_selection": plan.selection,
        "baseline_run_id": plan.baseline_run_id,
        "degrade_reason": REASON_CURRENT_RUN_FAILED,
        "degrade_detail": {"upstream_reason": plan.degrade_reason},
        "anchored_at": plan.anchored_at,
        "comparable_count": 0,
        "incomparable_count": 0,
        "metrics": {},
        "summary_text": _summary_text(
            plan, {}, degraded=True, degrade_reason=REASON_CURRENT_RUN_FAILED
        ),
    }
