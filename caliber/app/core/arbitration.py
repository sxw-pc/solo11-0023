"""Cross-implementation consensus arbitration.

The comparator answers "is this metric consistent?" for the whole metric.
This module answers a narrower follow-up question: *which implementations
agree with each other, which one(s) deviate, and where?*

Only implementations that were evaluated successfully take part in the
vote.  Failed / rejected implementations are reported as non-arbitrable and
never become dissenters.

Two strategies are available:

``impl_cluster`` (default)
    Build a pairwise agreement graph (agreement ratio over the aligned
    dimension union) and take the unique largest clique as the consensus
    group.  Complexity is O(n^2 * d).

``dimension_vote``
    Vote per dimension and derive each implementation's disagreement ratio
    against the per-dimension majority.  It is sensitive to local deviations,
    but the per-dimension partitions do not by themselves define a stable
    implementation group.

Even with the default ``impl_cluster`` strategy, the dissent dimensions are
expanded per dissenting implementation, and slight deviations inside the
consensus group are recorded separately under ``minority`` so a single
dimension deviation is never silently swallowed.
"""
from __future__ import annotations

from itertools import combinations
from typing import Any, Iterable, Mapping, Sequence

from ..errors import CaliberError
from ..models import ImplFailure
from .comparator import values_match

DEFAULT_CONSENSUS_THRESHOLD = 1.0
DEFAULT_STRATEGY = "impl_cluster"
SUPPORTED_STRATEGIES = ("impl_cluster", "dimension_vote")

CHOICE_RATIONALE = (
    "采用实现级聚类（impl_cluster）：先对每个实现两两计算维度一致率，"
    "把一致率不低于阈值的实现连边，取规模唯一最大的完全子图作为共识组。"
    "理由是逐维投票需要把各维度的分组再聚合成稳定的实现分组，维度一多组合数量会膨胀，"
    "且容易同时出现多个同等大小的候选组；实现级聚类为 O(n²·d)，结果唯一且可复现。"
    "为避免吞掉单维异议，仲裁结果仍对每个异议方展开 dissent_dimensions，"
    "并把共识组内部的轻微偏离单独记录在 minority 中。"
)

SWITCH_POINT = (
    "app/core/arbitration.py::arbitrate(strategy=...); 由 "
    "perform_run(arbitration_strategy=...)、CLI --arbitration-strategy、"
    "API POST /api/runs 的 arbitration.strategy、环境变量 "
    "CALIBER_ARBITRATION_STRATEGY 传入。当前支持 impl_cluster（默认）与 dimension_vote。"
)

ALTERNATIVES = {
    "dimension_vote": (
        "逐维多数投票：对局部偏差敏感，但各维度分组不能直接合并为稳定的实现分组，"
        "维度组合较多时容易出现多个候选组，快照中需要额外保存逐维分组结果。"
    )
}


def validate_threshold(threshold: float | None) -> float | None:
    """Return a validated threshold or ``None`` when arbitration is disabled."""
    if threshold is None:
        return None
    try:
        value = float(threshold)
    except (TypeError, ValueError) as exc:
        raise CaliberError("共识阈值必须是数值") from exc
    if not 0.0 <= value <= 1.0:
        raise CaliberError("共识阈值必须在 0 到 1 之间")
    return value


def validate_strategy(strategy: str | None) -> str:
    value = DEFAULT_STRATEGY if strategy is None else str(strategy)
    if value not in SUPPORTED_STRATEGIES:
        raise CaliberError(
            f"不支持的仲裁策略: {value}",
            code="validation_error",
            details={"supported": list(SUPPORTED_STRATEGIES)},
        )
    return value


def arbitrate(
    results: Mapping[str, Mapping[str, float]],
    failures: Iterable[ImplFailure | Mapping[str, Any]] = (),
    *,
    abs_tolerance: float = 1e-6,
    rel_tolerance: float = 0.0,
    threshold: float = DEFAULT_CONSENSUS_THRESHOLD,
    strategy: str = DEFAULT_STRATEGY,
    impl_order: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Build the arbitration payload stored in ``snapshot.result_json``.

    The function is defensive: an internal failure returns a degraded payload
    instead of raising, so a single metric can never abort a whole run.
    """
    threshold = validate_threshold(threshold)
    if threshold is None:
        raise CaliberError("仲裁未启用")
    strategy = validate_strategy(strategy)
    failure_items = [_failure_dict(item) for item in failures]
    order = _impl_order(results, impl_order, failure_items)

    try:
        if strategy == "dimension_vote":
            body = _arbitrate_dimension_vote(
                results, order, abs_tolerance, rel_tolerance, threshold
            )
        else:
            body = _arbitrate_impl_cluster(
                results, order, abs_tolerance, rel_tolerance, threshold
            )
    except Exception as exc:  # defensive: arbitration must not break the run
        body = {
            "status": "degraded",
            "degraded": {
                "code": "arbitration_error",
                "reason": "仲裁计算异常，已保留原有维度比对结果",
                "detail": f"{type(exc).__name__}: {exc}",
            },
            "consensus_group": [],
            "dissenters": [],
            "dissent_dimensions": [],
            "minority": [],
            "max_divergence": 0.0,
            "max_divergence_detail": None,
            "pairwise": [],
            "impl_classification": {
                impl_id: "unknown" for impl_id in order if impl_id in results
            },
        }

    arbitrable = [impl_id for impl_id in order if impl_id in results]
    classification = body.get("impl_classification", {})
    for item in failure_items:
        classification[item["impl_id"]] = "non_arbitrable"
    body.update(
        {
            "enabled": True,
            "strategy": strategy,
            "threshold": threshold,
            "tolerances": {"abs": float(abs_tolerance), "rel": float(rel_tolerance)},
            "selection": {
                "chosen": DEFAULT_STRATEGY,
                "active": strategy,
                "rationale": CHOICE_RATIONALE,
                "switch_point": SWITCH_POINT,
                "alternatives": dict(ALTERNATIVES),
            },
            "impl_order": order,
            "arbitrable_impl_count": len(arbitrable),
            "non_arbitrable": failure_items,
            "impl_classification": classification,
            "summary": {
                "impl_count": len(order),
                "arbitrable_impl_count": len(arbitrable),
                "consensus_count": len(body.get("consensus_group", [])),
                "dissenter_count": len(body.get("dissenters", [])),
                "non_arbitrable_count": len(failure_items),
                "dissent_dimension_count": len(body.get("dissent_dimensions", [])),
            },
        }
    )
    return body


# ---------------------------------------------------------------------------
# strategy: implementation-level clustering
# ---------------------------------------------------------------------------


def _arbitrate_impl_cluster(
    results: Mapping[str, Mapping[str, float]],
    order: list[str],
    abs_tolerance: float,
    rel_tolerance: float,
    threshold: float,
) -> dict[str, Any]:
    arbitrable = [impl_id for impl_id in order if impl_id in results]
    dims = _dimensions(results, arbitrable)
    pairwise = _pairwise(
        results, arbitrable, dims, abs_tolerance, rel_tolerance, threshold
    )
    max_divergence, max_detail = _max_divergence(pairwise)

    if len(arbitrable) == 0:
        return _degraded_body(
            "no_arbitrable_impls",
            "没有成功求值的实现，无法进行跨实现仲裁",
            pairwise=pairwise,
            max_divergence=max_divergence,
            max_detail=max_detail,
        )
    if len(arbitrable) == 1:
        return _degraded_body(
            "insufficient_impls",
            "可仲裁实现不足两个，无法形成共识组",
            pairwise=pairwise,
            max_divergence=max_divergence,
            max_detail=max_detail,
            classification={arbitrable[0]: "insufficient"},
        )

    neighbors = {impl_id: set() for impl_id in arbitrable}
    for item in pairwise:
        if item["agree"]:
            neighbors[item["left"]].add(item["right"])
            neighbors[item["right"]].add(item["left"])

    cliques = _dedupe_cliques(_maximal_cliques(arbitrable, neighbors))
    candidates = [item for item in cliques if len(item) >= 2]
    if not candidates:
        if len(arbitrable) == 2:
            return _degraded_body(
                "two_impl_disagreement",
                "两个实现互不一致，无法形成多数共识，降级为不作共识判定",
                pairwise=pairwise,
                max_divergence=max_divergence,
                max_detail=max_detail,
                dissenters=_unstructured_dissenters(
                    arbitrable, pairwise, "两个实现互不一致，无法形成多数共识"
                ),
                dissent_dimensions=_pairwise_mismatch_dimensions(pairwise),
                classification={impl_id: "disputed" for impl_id in arbitrable},
            )
        return _degraded_body(
            "no_consensus",
            "所有实现两两之间均不一致，无法形成共识组，降级为不作共识判定",
            pairwise=pairwise,
            max_divergence=max_divergence,
            max_detail=max_detail,
            dissenters=_unstructured_dissenters(
                arbitrable, pairwise, "所有实现两两不一致，未形成共识组"
            ),
            dissent_dimensions=_pairwise_mismatch_dimensions(pairwise),
            classification={impl_id: "disputed" for impl_id in arbitrable},
        )

    best_size = max(len(item) for item in candidates)
    best = [item for item in candidates if len(item) == best_size]
    if len(best) > 1:
        return _degraded_body(
            "no_majority",
            "存在多个同等规模且互不相容的候选共识组，无法唯一确定共识方，降级为不作共识判定",
            pairwise=pairwise,
            max_divergence=max_divergence,
            max_detail=max_detail,
            dissenters=_unstructured_dissenters(
                arbitrable, pairwise, "存在多个同等规模的候选共识组，未形成唯一共识"
            ),
            dissent_dimensions=_pairwise_mismatch_dimensions(pairwise),
            classification={impl_id: "disputed" for impl_id in arbitrable},
            candidate_groups=best,
        )

    consensus = sorted(best[0], key=order.index)
    dissenters = [impl_id for impl_id in arbitrable if impl_id not in consensus]
    dissent_entries = [
        _dissenter_entry(
            impl_id, results, consensus, dims, abs_tolerance, rel_tolerance
        )
        for impl_id in dissenters
    ]
    minority = _minority_entries(
        consensus, results, dims, abs_tolerance, rel_tolerance, order
    )
    dissent_dimensions = sorted(
        {
            detail["dimension"]
            for entry in dissent_entries
            for detail in entry["dissent_details"]
        }
    )
    classification = {impl_id: "consensus" for impl_id in consensus}
    classification.update({impl_id: "dissent" for impl_id in dissenters})
    return {
        "status": "ok",
        "degraded": None,
        "consensus_group": consensus,
        "dissenters": dissent_entries,
        "dissent_dimensions": dissent_dimensions,
        "minority": minority,
        "max_divergence": max_divergence,
        "max_divergence_detail": max_detail,
        "pairwise": pairwise,
        "candidate_groups": [],
        "impl_classification": classification,
    }


# ---------------------------------------------------------------------------
# strategy: per-dimension majority vote
# ---------------------------------------------------------------------------


def _arbitrate_dimension_vote(
    results: Mapping[str, Mapping[str, float]],
    order: list[str],
    abs_tolerance: float,
    rel_tolerance: float,
    threshold: float,
) -> dict[str, Any]:
    """Per-dimension majority vote.

    A dimension with no unique majority is skipped.  An implementation
    joins the consensus group when its agreement ratio over the voted
    dimensions reaches the threshold.  Dissent details are always expanded
    per implementation, and consensus members that deviate on a single
    dimension land in ``minority`` instead of being silently absorbed.
    """
    arbitrable = [impl_id for impl_id in order if impl_id in results]
    dims = _dimensions(results, arbitrable)
    pairwise = _pairwise(
        results, arbitrable, dims, abs_tolerance, rel_tolerance, threshold
    )
    max_divergence, max_detail = _max_divergence(pairwise)

    if len(arbitrable) == 0:
        return _degraded_body(
            "no_arbitrable_impls",
            "没有成功求值的实现，无法进行跨实现仲裁",
            pairwise=pairwise,
            max_divergence=max_divergence,
            max_detail=max_detail,
        )
    if len(arbitrable) == 1:
        return _degraded_body(
            "insufficient_impls",
            "可仲裁实现不足两个，无法形成共识组",
            pairwise=pairwise,
            max_divergence=max_divergence,
            max_detail=max_detail,
            classification={arbitrable[0]: "insufficient"},
        )

    majority: dict[str, set[str] | None] = {}
    references: dict[str, float | None] = {}
    for dimension in dims:
        groups = _value_groups(arbitrable, results, dimension, abs_tolerance, rel_tolerance)
        if not groups:
            majority[dimension] = None
            references[dimension] = None
            continue
        largest = max(len(group) for group in groups)
        winners = [group for group in groups if len(group) == largest]
        if len(winners) != 1:
            majority[dimension] = None
            references[dimension] = None
            continue
        members = winners[0]
        majority[dimension] = set(members)
        references[dimension] = _median(
            [float(results[impl_id][dimension]) for impl_id in members]
        )

    if all(voters is None for voters in majority.values()):
        if len(arbitrable) == 2:
            code = "two_impl_disagreement"
            reason = "两个实现互不一致，无法形成多数共识，降级为不作共识判定"
            detail_reason = "两个实现互不一致，无法形成多数共识"
        else:
            code = "no_consensus"
            reason = "逐维投票在每个维度上都没有唯一多数，无法形成共识组，降级为不作共识判定"
            detail_reason = "逐维投票在每个维度上都没有唯一多数"
        return _degraded_body(
            code,
            reason,
            pairwise=pairwise,
            max_divergence=max_divergence,
            max_detail=max_detail,
            dissenters=_unstructured_dissenters(
                arbitrable, pairwise, detail_reason
            ),
            dissent_dimensions=_pairwise_mismatch_dimensions(pairwise),
            classification={impl_id: "disputed" for impl_id in arbitrable},
        )

    dissent_details: dict[str, list[dict[str, Any]]] = {impl_id: [] for impl_id in arbitrable}
    considered: dict[str, int] = {impl_id: 0 for impl_id in arbitrable}
    for dimension in dims:
        voters = majority[dimension]
        if voters is None:
            continue
        reference = references[dimension]
        for impl_id in arbitrable:
            considered[impl_id] += 1
            if impl_id in voters:
                continue
            if dimension not in results[impl_id]:
                detail = {
                    "dimension": dimension,
                    "kind": "missing_dimension",
                    "impl_value": None,
                    "consensus_value": reference,
                    "delta": None,
                }
            else:
                value = float(results[impl_id][dimension])
                if values_match(value, reference or 0.0, abs_tolerance, rel_tolerance):
                    continue
                detail = {
                    "dimension": dimension,
                    "kind": "value_mismatch",
                    "impl_value": value,
                    "consensus_value": reference,
                    "delta": abs(value - (reference or 0.0)),
                }
            dissent_details[impl_id].append(detail)

    ratios = {
        impl_id: (
            len(dissent_details[impl_id]) / considered[impl_id]
            if considered[impl_id]
            else 0.0
        )
        for impl_id in arbitrable
    }
    consensus = [
        impl_id
        for impl_id in arbitrable
        if 1.0 - ratios[impl_id] >= threshold
    ]
    if len(consensus) < 2:
        code = "two_impl_disagreement" if len(arbitrable) == 2 else "no_consensus"
        reason = (
            "两个实现互不一致，无法形成多数共识，降级为不作共识判定"
            if code == "two_impl_disagreement"
            else "逐维投票未形成至少两个实现组成的共识组，降级为不作共识判定"
        )
        return _degraded_body(
            code,
            reason,
            pairwise=pairwise,
            max_divergence=max_divergence,
            max_detail=max_detail,
            dissenters=_unstructured_dissenters(arbitrable, pairwise, reason),
            dissent_dimensions=_pairwise_mismatch_dimensions(pairwise),
            classification={impl_id: "disputed" for impl_id in arbitrable},
        )

    dissenters = [impl_id for impl_id in arbitrable if impl_id not in consensus]
    dissent_entries = []
    for impl_id in dissenters:
        details = sorted(
            dissent_details[impl_id], key=lambda item: item["dimension"]
        )
        deltas = [
            item["delta"] for item in details if item["delta"] is not None
        ]
        dissent_entries.append(
            {
                "impl_id": impl_id,
                "dissent_dimensions": [item["dimension"] for item in details],
                "dissent_details": details,
                "max_divergence": max(deltas) if deltas else 0.0,
                "agreement_ratio": round(1.0 - ratios[impl_id], 6),
                "reason": "未跟随逐维多数投票",
            }
        )
    minority = [
        {
            "impl_id": impl_id,
            "minority_dimensions": [item["dimension"] for item in dissent_details[impl_id]],
            "minority_details": sorted(
                dissent_details[impl_id], key=lambda item: item["dimension"]
            ),
        }
        for impl_id in consensus
        if dissent_details[impl_id]
    ]
    dissent_dimensions = sorted(
        {
            detail["dimension"]
            for entry in dissent_entries
            for detail in entry["dissent_details"]
        }
    )
    classification = {impl_id: "consensus" for impl_id in consensus}
    classification.update({impl_id: "dissent" for impl_id in dissenters})
    return {
        "status": "ok",
        "degraded": None,
        "consensus_group": consensus,
        "dissenters": dissent_entries,
        "dissent_dimensions": dissent_dimensions,
        "minority": minority,
        "max_divergence": max_divergence,
        "max_divergence_detail": max_detail,
        "pairwise": pairwise,
        "candidate_groups": [],
        "impl_classification": classification,
    }


# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------


def _impl_order(
    results: Mapping[str, Mapping[str, float]],
    impl_order: Sequence[str] | None,
    failure_items: list[dict[str, Any]],
) -> list[str]:
    if impl_order is not None:
        ordered = [impl_id for impl_id in impl_order]
    else:
        ordered = list(results)
    for item in failure_items:
        if item["impl_id"] not in ordered:
            ordered.append(item["impl_id"])
    for impl_id in results:
        if impl_id not in ordered:
            ordered.append(impl_id)
    return ordered


def _failure_dict(item: ImplFailure | Mapping[str, Any]) -> dict[str, Any]:
    if hasattr(item, "to_dict"):
        payload = item.to_dict()  # type: ignore[union-attr]
    else:
        payload = dict(item)
    payload.setdefault("stage", "evaluation")
    payload.setdefault("impl_id", "")
    payload.setdefault("source", "")
    payload.setdefault("sql_file", "")
    payload.setdefault("code", "evaluation_error")
    payload.setdefault("reason", "实现求值失败")
    return {
        "impl_id": str(payload["impl_id"]),
        "source": str(payload["source"]),
        "sql_file": str(payload["sql_file"]),
        "stage": str(payload["stage"]),
        "code": str(payload["code"]),
        "reason": str(payload["reason"]),
    }


def _dimensions(
    results: Mapping[str, Mapping[str, float]], impls: Sequence[str]
) -> list[str]:
    return sorted(
        {dimension for impl_id in impls for dimension in results[impl_id]}
    )


def _pairwise(
    results: Mapping[str, Mapping[str, float]],
    impls: Sequence[str],
    dims: Sequence[str],
    abs_tolerance: float,
    rel_tolerance: float,
    threshold: float,
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for left, right in combinations(impls, 2):
        mismatches: list[dict[str, Any]] = []
        max_delta = 0.0
        max_delta_dimension: str | None = None
        considered = 0
        for dimension in dims:
            left_has = dimension in results[left]
            right_has = dimension in results[right]
            if not left_has and not right_has:
                continue
            considered += 1
            if not left_has or not right_has:
                mismatches.append(
                    {
                        "dimension": dimension,
                        "kind": "missing_dimension",
                        "delta": None,
                    }
                )
                continue
            delta = abs(float(results[left][dimension]) - float(results[right][dimension]))
            if delta > max_delta:
                max_delta = delta
                max_delta_dimension = dimension
            if not values_match(
                float(results[left][dimension]),
                float(results[right][dimension]),
                abs_tolerance,
                rel_tolerance,
            ):
                mismatches.append(
                    {
                        "dimension": dimension,
                        "kind": "value_mismatch",
                        "delta": delta,
                    }
                )
        ratio = len(mismatches) / considered if considered else 0.0
        agreement = 1.0 - ratio
        entries.append(
            {
                "left": left,
                "right": right,
                "considered_dimensions": considered,
                "mismatch_count": len(mismatches),
                "agreement_ratio": round(agreement, 6),
                "agree": agreement >= threshold,
                "mismatch_dimensions": mismatches,
                "max_delta": max_delta,
                "max_delta_dimension": max_delta_dimension,
            }
        )
    return entries


def _max_divergence(
    pairwise: Sequence[Mapping[str, Any]]
) -> tuple[float, dict[str, Any] | None]:
    best = 0.0
    detail: dict[str, Any] | None = None
    for item in pairwise:
        delta = item.get("max_delta")
        if delta is None or delta <= best:
            continue
        best = float(delta)
        detail = {
            "left": item["left"],
            "right": item["right"],
            "dimension": item.get("max_delta_dimension"),
            "delta": float(delta),
        }
    return best, detail


def _maximal_cliques(
    nodes: Sequence[str], neighbors: Mapping[str, set[str]]
) -> list[list[str]]:
    ordered = sorted(nodes)
    if len(ordered) > 20:  # pragma: no cover - guard for unusual wide metrics
        return _greedy_cliques(ordered, neighbors)
    found: list[list[str]] = []

    def visit(chosen: set[str], candidates: set[str], excluded: set[str]) -> None:
        if not candidates and not excluded:
            found.append(sorted(chosen))
            return
        union = candidates | excluded
        pivot = max(union, key=lambda item: len(candidates & neighbors[item]))
        for node in sorted(candidates - neighbors[pivot]):
            visit(
                chosen | {node},
                candidates & neighbors[node],
                excluded & neighbors[node],
            )
            candidates.remove(node)
            excluded.add(node)

    visit(set(), set(ordered), set())
    return found


def _greedy_cliques(  # pragma: no cover - only used for >20 implementations
    nodes: Sequence[str], neighbors: Mapping[str, set[str]]
) -> list[list[str]]:
    groups: list[list[str]] = []
    for seed in nodes:
        group = [seed]
        for candidate in nodes:
            if candidate in group:
                continue
            if all(candidate in neighbors[member] for member in group):
                group.append(candidate)
        groups.append(sorted(group))
    return groups


def _dedupe_cliques(cliques: Sequence[Sequence[str]]) -> list[list[str]]:
    unique: list[list[str]] = []
    for clique in sorted({tuple(sorted(item)) for item in cliques}, key=lambda item: (-len(item), item)):
        if not any(set(clique) <= set(existing) for existing in unique):
            unique.append(list(clique))
    return unique


def _value_groups(
    impls: Sequence[str],
    results: Mapping[str, Mapping[str, float]],
    dimension: str,
    abs_tolerance: float,
    rel_tolerance: float,
) -> list[list[str]]:
    groups: list[list[str]] = []
    representatives: list[float] = []
    for impl_id in impls:
        if dimension not in results[impl_id]:
            continue
        value = float(results[impl_id][dimension])
        for index, representative in enumerate(representatives):
            if values_match(value, representative, abs_tolerance, rel_tolerance):
                groups[index].append(impl_id)
                break
        else:
            groups.append([impl_id])
            representatives.append(value)
    return groups


def _median(values: Sequence[float]) -> float:
    ordered = sorted(float(item) for item in values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _dissenter_entry(
    impl_id: str,
    results: Mapping[str, Mapping[str, float]],
    consensus: Sequence[str],
    dims: Sequence[str],
    abs_tolerance: float,
    rel_tolerance: float,
    reason: str = "偏离共识组",
) -> dict[str, Any]:
    details = _details_against_group(
        impl_id, results, consensus, dims, abs_tolerance, rel_tolerance
    )
    considered = sum(
        1
        for dimension in dims
        if dimension in results[impl_id]
        or any(dimension in results[member] for member in consensus)
    )
    deltas = [item["delta"] for item in details if item["delta"] is not None]
    return {
        "impl_id": impl_id,
        "dissent_dimensions": [item["dimension"] for item in details],
        "dissent_details": details,
        "max_divergence": max(deltas) if deltas else 0.0,
        "agreement_ratio": round(
            1.0 - (len(details) / considered if considered else 0.0), 6
        ),
        "reason": reason,
    }


def _details_against_group(
    impl_id: str,
    results: Mapping[str, Mapping[str, float]],
    group: Sequence[str],
    dims: Sequence[str],
    abs_tolerance: float,
    rel_tolerance: float,
) -> list[dict[str, Any]]:
    details: list[dict[str, Any]] = []
    for dimension in dims:
        impl_has = dimension in results[impl_id]
        references = [
            float(results[member][dimension])
            for member in group
            if dimension in results[member]
        ]
        if not references:
            if impl_has:
                details.append(
                    {
                        "dimension": dimension,
                        "kind": "extra_dimension",
                        "impl_value": float(results[impl_id][dimension]),
                        "consensus_value": None,
                        "delta": None,
                    }
                )
            continue
        reference = _median(references)
        if not impl_has:
            details.append(
                {
                    "dimension": dimension,
                    "kind": "missing_dimension",
                    "impl_value": None,
                    "consensus_value": reference,
                    "delta": None,
                }
            )
            continue
        value = float(results[impl_id][dimension])
        if not values_match(value, reference, abs_tolerance, rel_tolerance):
            details.append(
                {
                    "dimension": dimension,
                    "kind": "value_mismatch",
                    "impl_value": value,
                    "consensus_value": reference,
                    "delta": abs(value - reference),
                }
            )
    return details


def _minority_entries(
    consensus: Sequence[str],
    results: Mapping[str, Mapping[str, float]],
    dims: Sequence[str],
    abs_tolerance: float,
    rel_tolerance: float,
    order: Sequence[str],
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for impl_id in sorted(consensus, key=order.index):
        others = [member for member in consensus if member != impl_id]
        if not others:
            continue
        details = _details_against_group(
            impl_id, results, others, dims, abs_tolerance, rel_tolerance
        )
        if details:
            entries.append(
                {
                    "impl_id": impl_id,
                    "minority_dimensions": [item["dimension"] for item in details],
                    "minority_details": details,
                }
            )
    return entries


def _unstructured_dissenters(
    impls: Sequence[str],
    pairwise: Sequence[Mapping[str, Any]],
    reason: str,
) -> list[dict[str, Any]]:
    """Explain dissent without a consensus reference (degraded cases)."""
    details: dict[str, dict[str, dict[str, Any]]] = {impl_id: {} for impl_id in impls}
    deltas: dict[str, float] = {impl_id: 0.0 for impl_id in impls}
    for item in pairwise:
        for impl_id in (item["left"], item["right"]):
            for mismatch in item["mismatch_dimensions"]:
                details[impl_id][mismatch["dimension"]] = mismatch
            delta = item.get("max_delta") or 0.0
            deltas[impl_id] = max(deltas[impl_id], float(delta))
    entries: list[dict[str, Any]] = []
    for impl_id in impls:
        items = []
        for key in sorted(details[impl_id]):
            mismatch = dict(details[impl_id][key])
            mismatch.setdefault("impl_value", None)
            mismatch.setdefault("consensus_value", None)
            items.append(mismatch)
        entries.append(
            {
                "impl_id": impl_id,
                "dissent_dimensions": [item["dimension"] for item in items],
                "dissent_details": items,
                "max_divergence": deltas[impl_id],
                "agreement_ratio": 0.0,
                "reason": reason,
            }
        )
    return entries


def _pairwise_mismatch_dimensions(
    pairwise: Sequence[Mapping[str, Any]]
) -> list[str]:
    return sorted(
        {
            mismatch["dimension"]
            for item in pairwise
            for mismatch in item["mismatch_dimensions"]
        }
    )


def _degraded_body(
    code: str,
    reason: str,
    *,
    pairwise: Sequence[Mapping[str, Any]],
    max_divergence: float,
    max_detail: dict[str, Any] | None,
    dissenters: Sequence[Mapping[str, Any]] | None = None,
    dissent_dimensions: Sequence[str] | None = None,
    classification: Mapping[str, str] | None = None,
    candidate_groups: Sequence[Sequence[str]] | None = None,
) -> dict[str, Any]:
    return {
        "status": "degraded",
        "degraded": {"code": code, "reason": reason, "detail": None},
        "consensus_group": [],
        "dissenters": list(dissenters or []),
        "dissent_dimensions": list(dissent_dimensions or []),
        "minority": [],
        "max_divergence": max_divergence,
        "max_divergence_detail": max_detail,
        "pairwise": list(pairwise),
        "candidate_groups": [list(group) for group in (candidate_groups or [])],
        "impl_classification": dict(classification or {}),
    }
