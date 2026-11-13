"""维度裁剪：差异维度的确定性排序、影响权重与报告视图过滤。

排序结果在 perform_run 落库时写入快照摘要（summary_json.dimension_ranking），
导出报告时直接读取，不再触碰 result_json 明细；旧快照缺少排序时按同一套
确定性规则补算并回写摘要。result_json 原始明细始终不改写。

确定性保证：排序键是全序的（档位、差值、维度名），同一批输入重复计算结果
完全一致；None 与非有限数（NaN/±Inf）有固定排位，不会随机漂移。
"""
from __future__ import annotations

import math
from typing import Any, Mapping

from ..errors import ReportError

RANKING_VERSION = 1
VIEWS = ("all", "top", "dimension")
DEFAULT_TOP_N = 10

SORT_RULE = (
    "按 |max_delta| 降序；非有限值（NaN/±Inf）排在有限值之后，"
    "缺失值（None，如缺失维度）排在最后；同档内按维度名升序，维度名唯一故全序确定"
)
WEIGHT_RULE = "weight = |max_delta| / 所有有限 |max_delta| 之和；缺失或非有限维度权重为 null"
DEGRADED_NOTICE = (
    "快照缺少差异明细（result_json.diffs），维度排序已降级为按维度名排序，"
    "不包含差值与影响权重"
)


def _normalize_delta(delta: Any) -> float | None:
    """把 max_delta 归一为 float 或 None；布尔、字符串等异常类型按缺失处理。"""
    if isinstance(delta, bool) or not isinstance(delta, (int, float)):
        return None
    return float(delta)


def _bucket(delta: float | None) -> int:
    """排位档位：0=有限数值，1=非有限数值（NaN/±Inf），2=缺失（None）。"""
    if delta is None:
        return 2
    return 0 if math.isfinite(delta) else 1


def rank_diffs(diffs: list[Any], *, generated_by: str) -> dict[str, Any]:
    """对差异明细按 |max_delta| 做确定性稳定排序，并计算每维度影响权重。"""
    pairs: list[tuple[str, Any, float | None]] = []
    seen: set[str] = set()
    for entry in diffs:
        if not isinstance(entry, Mapping):
            continue
        name = entry.get("dimension")
        if name is None:
            continue
        key = str(name)
        if key in seen:  # 维度名本应唯一；上游异常时保留先出现者以维持全序
            continue
        seen.add(key)
        pairs.append((key, entry.get("kind"), _normalize_delta(entry.get("max_delta"))))
    ordered = sorted(
        pairs,
        key=lambda item: (
            _bucket(item[2]),
            -abs(item[2]) if _bucket(item[2]) == 0 else 0.0,
            item[0],
        ),
    )
    total = sum(abs(delta) for _, _, delta in ordered if _bucket(delta) == 0)
    entries: list[dict[str, Any]] = []
    for rank, (name, kind, delta) in enumerate(ordered, start=1):
        weight: float | None = None
        if delta is not None and _bucket(delta) == 0 and total > 0:
            weight = abs(delta) / total
        entries.append(
            {
                "rank": rank,
                "dimension": name,
                "kind": kind if isinstance(kind, str) else None,
                "max_delta": delta,
                "weight": weight,
            }
        )
    return {
        "version": RANKING_VERSION,
        "generated_by": generated_by,
        "degraded": False,
        "notice": None,
        "sort_rule": SORT_RULE,
        "weight_rule": WEIGHT_RULE,
        "total_delta": total,
        "entries": entries,
    }


def degrade_ranking(dimensions: Any, *, generated_by: str) -> dict[str, Any]:
    """缺少差异明细时的降级排序：只按维度名升序，不带差值与权重。"""
    names: list[str] = []
    if isinstance(dimensions, list):
        seen: set[str] = set()
        for item in dimensions:
            name = str(item)
            if name not in seen:
                seen.add(name)
                names.append(name)
    names.sort()
    entries = [
        {"rank": rank, "dimension": name, "kind": None, "max_delta": None, "weight": None}
        for rank, name in enumerate(names, start=1)
    ]
    return {
        "version": RANKING_VERSION,
        "generated_by": generated_by,
        "degraded": True,
        "notice": DEGRADED_NOTICE,
        "sort_rule": "缺少差异明细，仅按维度名升序",
        "weight_rule": None,
        "total_delta": None,
        "entries": entries,
    }


def rank_snapshot_result(result: Any, *, generated_by: str) -> dict[str, Any]:
    """按解码后的快照 result 计算排序；缺明细时自动降级，绝不抛错。"""
    diffs = result.get("diffs") if isinstance(result, Mapping) else None
    if isinstance(diffs, list):
        return rank_diffs(diffs, generated_by=generated_by)
    dimensions = result.get("dimensions") if isinstance(result, Mapping) else None
    return degrade_ranking(dimensions, generated_by=generated_by)


def normalize_view(view: Any = "all", top: Any = None, dimension: Any = None) -> dict[str, Any]:
    """校验并归一化三种视图参数：all / top N / dimension X。"""
    mode = str(view or "all").strip().lower()
    if mode not in VIEWS:
        raise ReportError(f"未知的维度视图: {view}（可选: {', '.join(VIEWS)}）")
    top_n: int | None = None
    if top is not None and top != "":
        try:
            top_n = int(top)
        except (TypeError, ValueError) as exc:
            raise ReportError(f"top 必须是正整数: {top!r}") from exc
        if top_n < 1:
            raise ReportError(f"top 必须是正整数: {top!r}")
    if mode == "top" and top_n is None:
        top_n = DEFAULT_TOP_N
    dim = str(dimension) if dimension is not None else None
    if mode == "dimension" and not (dim and dim.strip()):
        raise ReportError("dimension 视图必须通过 dimension 参数指定维度名")
    if mode == "top":
        label = f"前 {top_n} 个（按 |差值| 降序）"
    elif mode == "dimension":
        label = f"仅维度「{dim}」"
    else:
        label = "全部"
    return {"mode": mode, "top": top_n, "dimension": dim, "label": label}


def apply_view(
    diffs: list[dict[str, Any]],
    ranking: Mapping[str, Any],
    view: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """按视图过滤差异明细；top 视图按排序名次输出，dimension 视图取单一维度。"""
    total = len(diffs)
    mode = view["mode"]
    if mode == "top":
        top_n = int(view["top"])
        order = [entry["dimension"] for entry in ranking.get("entries", [])][:top_n]
        by_name: dict[str, dict[str, Any]] = {}
        for diff in diffs:
            name = diff.get("dimension")
            if name is not None and str(name) not in by_name:
                by_name[str(name)] = diff
        selected = [by_name[name] for name in order if name in by_name]
        return selected, {"shown": len(selected), "total": total}
    if mode == "dimension":
        target = view["dimension"]
        selected = [diff for diff in diffs if diff.get("dimension") == target]
        return selected, {"shown": len(selected), "total": total}
    return list(diffs), {"shown": total, "total": total}
