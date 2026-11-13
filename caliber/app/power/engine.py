"""Assessment engine: path selection, significance testing and planning.

The engine is pure and deterministic.  It takes normalized observations and
settings, and returns a JSON-serializable result.  No wall-clock timestamp is
included, so running the same inputs twice yields byte-identical JSON (given
the same settings and input digests).
"""
from __future__ import annotations

from itertools import combinations
from typing import Any, Iterable

from .models import AssessmentSettings, Issue, LoadedArtifacts, RuleObservation
from .stats import (
    ExactBudgetExceeded,
    chi_square_homogeneity,
    expected_cell_minimum,
    fisher_exact_states,
    fisher_exact_two_sided,
    holm_adjust,
    min_sparse_cell,
    sample_size_for_effect,
    two_proportion_z_test,
    wilson_interval,
)

REPORT_VERSION = "1.0"


def _group_observations(observations: Iterable[RuleObservation]) -> dict[str, list[RuleObservation]]:
    grouped: dict[str, list[RuleObservation]] = {}
    for item in observations:
        grouped.setdefault(item.rule_key, []).append(item)
    for items in grouped.values():
        items.sort(key=lambda item: item.dataset)
    return {key: grouped[key] for key in sorted(grouped)}


def _select_path(
    groups: list[RuleObservation], settings: AssessmentSettings
) -> dict[str, Any]:
    """Decide between exact and normal-approximation testing, with reasons."""
    counts = [(item.hits, item.total) for item in groups]
    expected_min = expected_cell_minimum(counts)
    observed_min = min_sparse_cell(counts)
    threshold = settings.min_expected_cell
    # A zero expected/observed cell makes the normal approximation undefined,
    # so it always routes to the exact path, even if the threshold is lowered.
    sparse = (
        expected_min <= 0.0
        or observed_min <= 0
        or expected_min < threshold
        or observed_min < threshold
    )
    indicators: dict[str, Any] = {
        "dataset_count": len(groups),
        "total_samples": sum(item.total for item in groups),
        "min_group_total": min((item.total for item in groups), default=0),
        "min_expected_cell": round(expected_min, 6),
        "min_observed_cell": observed_min,
        "expected_cell_threshold": threshold,
        "state_budget": settings.exact_state_budget,
    }
    sparse_reasons: list[str] = []
    if expected_min <= 0.0 or expected_min < threshold:
        sparse_reasons.append(
            f"最小预期格 {expected_min:.3g} 低于阈值 {threshold:g}"
        )
    if observed_min <= 0:
        sparse_reasons.append("存在零观测格，正态近似无定义")
    elif observed_min < threshold:
        sparse_reasons.append(
            f"最小观测格 {observed_min} 低于阈值 {threshold:g}"
        )
    sparse_reason = "；".join(sparse_reasons)
    if len(groups) < 2:
        return {
            "path": "descriptive",
            "method": "none",
            "reason_code": "single_dataset",
            "reason": "仅有一个数据集，无法做跨数据集对照，只输出描述性统计。",
            "indicators": indicators,
            "degraded": False,
            "degradation": None,
        }
    if not sparse:
        method = "two_proportion_z" if len(groups) == 2 else "chi_square"
        return {
            "path": "normal",
            "method": method,
            "reason_code": "sufficient_cells",
            "reason": (
                f"最小预期格 {expected_min:.3g}、最小观测格 {observed_min} "
                f"均不低于阈值 {threshold:g}，正态近似可用。"
            ),
            "indicators": indicators,
            "degraded": False,
            "degradation": None,
        }

    # Sparse data: count exact-test states before committing to the exact path.
    pairs = list(combinations(range(len(groups)), 2))
    state_count = 0
    for left, right in pairs:
        hits_a, total_a = counts[left]
        hits_b, total_b = counts[right]
        state_count += fisher_exact_states(hits_a, total_a, hits_b, total_b)
    indicators["state_count"] = state_count
    indicators["pair_count"] = len(pairs)

    if state_count > settings.exact_state_budget:
        method = "two_proportion_z" if len(groups) == 2 else "chi_square"
        return {
            "path": "normal",
            "method": method,
            "reason_code": "exact_budget_exceeded",
            "reason": (
                f"数据稀疏（{sparse_reason}），本应使用精确检验，"
                f"但精确枚举需要 {state_count} 个状态，超过预算 "
                f"{settings.exact_state_budget}，已降级为正态近似 {method}，"
                f"该结论的可靠性下降，建议缩小样本或在更高算力环境重跑。"
            ),
            "indicators": indicators,
            "degraded": True,
            "degradation": "exact_budget_exceeded",
        }
    method = "fisher_exact" if len(groups) == 2 else "fisher_exact_pairwise"
    return {
        "path": "exact",
        "method": method,
        "reason_code": "sparse_cells",
        "reason": (
            f"数据稀疏（{sparse_reason}），正态近似可能失真，改用精确检验"
            f"（枚举 {state_count} 个状态，预算 {settings.exact_state_budget}）。"
        ),
        "indicators": indicators,
        "degraded": False,
        "degradation": None,
    }


def _pairwise_exact(
    groups: list[RuleObservation], settings: AssessmentSettings
) -> list[dict[str, Any]]:
    pairs: list[dict[str, Any]] = []
    raw_p: list[float] = []
    for left, right in combinations(range(len(groups)), 2):
        first, second = groups[left], groups[right]
        p_value, states = fisher_exact_two_sided(
            first.hits,
            first.total,
            second.hits,
            second.total,
            state_budget=settings.exact_state_budget,
        )
        raw_p.append(p_value)
        pairs.append(
            {
                "datasets": [first.dataset, second.dataset],
                "hits": [first.hits, second.hits],
                "totals": [first.total, second.total],
                "hit_rates": [first.hit_rate, second.hit_rate],
                "p_value": p_value,
                "enumerated_states": states,
            }
        )
    adjusted = holm_adjust(raw_p)
    for item, value in zip(pairs, adjusted):
        item["adjusted_p_value"] = value
        item["significant"] = value < settings.alpha
    return pairs


def _recommendation(
    groups: list[RuleObservation], settings: AssessmentSettings
) -> dict[str, Any]:
    """Per-dataset and pooled sample-size recommendations for one rule.

    When a dataset's observed hit rate is exactly 0 or 1 the normal
    approximation has zero variance there; that dataset's plan falls back to
    the pooled rate and says so, instead of recommending a misleading floor.
    """
    pooled_hits = sum(item.hits for item in groups)
    pooled_total = sum(item.total for item in groups)
    pooled_rate = pooled_hits / pooled_total if pooled_total else 0.0
    per_dataset = []
    for item in groups:
        boundary = item.hit_rate <= 0.0 or item.hit_rate >= 1.0
        baseline = pooled_rate if boundary else item.hit_rate
        baseline_source = "pooled_fallback" if boundary else "dataset"
        plan = sample_size_for_effect(
            baseline,
            alpha=settings.alpha,
            power=settings.power,
            min_effect=settings.min_effect,
            min_expected_cell=settings.min_expected_cell,
            minimum_total=settings.min_group_total,
        )
        per_dataset.append(
            {
                "dataset": item.dataset,
                "observed_hit_rate": item.hit_rate,
                "observed_total": item.total,
                "planning_baseline_rate": baseline,
                "baseline_source": baseline_source,
                "recommended_total": plan["recommended_total_per_dataset"],
                "below_recommended": item.total < plan["recommended_total_per_dataset"],
                "plan": plan,
            }
        )
    pooled_plan = sample_size_for_effect(
        pooled_rate,
        alpha=settings.alpha,
        power=settings.power,
        min_effect=settings.min_effect,
        min_expected_cell=settings.min_expected_cell,
        minimum_total=settings.min_group_total,
    )
    recommended = max(
        [item["recommended_total"] for item in per_dataset]
        + [pooled_plan["recommended_total_per_dataset"]]
    )
    return {
        "pooled_hit_rate": pooled_rate,
        "min_effect": settings.min_effect,
        "alpha": settings.alpha,
        "power": settings.power,
        "recommended_total_per_dataset": recommended,
        "underpowered": any(item["below_recommended"] for item in per_dataset),
        "per_dataset": per_dataset,
        "pooled_plan": pooled_plan,
    }


def _boundary_warnings(rule_key: str, groups: list[RuleObservation]) -> list[Issue]:
    """Warn about boundary hit rates and all-boundary pooled planning."""
    warnings: list[Issue] = []
    for item in groups:
        if item.hits == 0 or item.hits == item.total:
            warnings.append(
                Issue(
                    "boundary_rate",
                    f"规则 {rule_key!r} 在数据集 {item.dataset!r} 上的命中率为 "
                    f"{item.hit_rate:.0%}（{item.hits}/{item.total}），处于边界；"
                    "样本量建议已改用合并命中率作为基线",
                    "warning",
                    dataset=item.dataset,
                    rule=rule_key,
                )
            )
    if groups:
        pooled_hits = sum(item.hits for item in groups)
        pooled_total = sum(item.total for item in groups)
        if pooled_total and (pooled_hits == 0 or pooled_hits == pooled_total):
            warnings.append(
                Issue(
                    "pooled_boundary_rate",
                    f"规则 {rule_key!r} 在所有数据集上的合并命中率均为边界值"
                    f"（{pooled_hits}/{pooled_total}），最小样本量建议仅作粗略参考；"
                    "建议先确认规则本身是否过严或过宽",
                    "warning",
                    rule=rule_key,
                )
            )
    return warnings


def _assess_rule(
    rule_key: str,
    groups: list[RuleObservation],
    settings: AssessmentSettings,
) -> tuple[dict[str, Any], list[Issue]]:
    """Assess one rule.

    The final significance verdict is left as ``"pending"`` for multi-dataset
    rules; :func:`assess` fills it after the across-rule correction so that
    the verdict and the adjusted p-value can never disagree.
    """
    name = next((item.name for item in groups if item.name), None)
    selection = _select_path(groups, settings)
    counts = [(item.hits, item.total) for item in groups]
    observed_min = min_sparse_cell(counts)

    datasets = []
    for item in groups:
        low, high = wilson_interval(item.hits, item.total, settings.confidence_level)
        datasets.append(
            {
                "dataset": item.dataset,
                "artifact": item.artifact,
                "hits": item.hits,
                "passes": item.passes,
                "total": item.total,
                "hit_rate": item.hit_rate,
                "pass_rate": item.pass_rate,
                "confidence_interval": {
                    "level": settings.confidence_level,
                    "low": low,
                    "high": high,
                },
            }
        )

    test: dict[str, Any] = {
        "path": selection["path"],
        "method": selection["method"],
        "reason_code": selection["reason_code"],
        "reason": selection["reason"],
        "indicators": selection["indicators"],
        "degraded": selection["degraded"],
        "degradation": selection["degradation"],
        "statistic": None,
        "degrees_of_freedom": None,
        "p_value": None,
        "within_rule_adjusted_p": None,
        "adjusted_p_value": None,
        "alpha": settings.alpha,
        "pairwise": [],
    }
    warnings: list[Issue] = _boundary_warnings(rule_key, groups)

    if len(groups) < 2:
        verdict = "no_control"
        interpretation = (
            "仅有一个数据集，无法做跨数据集显著性判断（无对照）。"
            "下列区间与样本量建议可用于后续补充对照数据集。"
        )
    else:
        if selection["path"] == "exact":
            if len(groups) == 2:
                first, second = groups
                try:
                    p_value, states = fisher_exact_two_sided(
                        first.hits,
                        first.total,
                        second.hits,
                        second.total,
                        state_budget=settings.exact_state_budget,
                    )
                except ExactBudgetExceeded as exc:  # defensive; path check covers it
                    raise AssertionError("路径选择与实际枚举不一致") from exc
                test["p_value"] = p_value
                test["within_rule_adjusted_p"] = p_value
                test["pairwise"] = [
                    {
                        "datasets": [first.dataset, second.dataset],
                        "hits": [first.hits, second.hits],
                        "totals": [first.total, second.total],
                        "hit_rates": [first.hit_rate, second.hit_rate],
                        "p_value": p_value,
                        "adjusted_p_value": p_value,
                        "significant": p_value < settings.alpha,
                        "enumerated_states": states,
                    }
                ]
            else:
                test["pairwise"] = _pairwise_exact(groups, settings)
                test["p_value"] = min(item["p_value"] for item in test["pairwise"])
                test["within_rule_adjusted_p"] = min(
                    item["adjusted_p_value"] for item in test["pairwise"]
                )
        else:
            if len(groups) == 2:
                first, second = groups
                statistic, p_value = two_proportion_z_test(
                    first.hits, first.total, second.hits, second.total
                )
                test["statistic"] = statistic
                test["p_value"] = p_value
            else:
                statistic, degrees, p_value = chi_square_homogeneity(counts)
                test["statistic"] = statistic
                test["degrees_of_freedom"] = degrees
                test["p_value"] = p_value
            test["within_rule_adjusted_p"] = p_value
        verdict = "pending"
        interpretation = ""

    if verdict == "pending":
        # Final decision is filled in after across-rule correction, so that the
        # per-rule verdict and adjusted p-value cannot disagree.
        interpretation = ""
    if selection["degraded"]:
        warnings.append(
            Issue(
                "exact_downgraded",
                f"规则 {rule_key!r}：{selection['reason']}",
                "warning",
                rule=rule_key,
                dataset=None,
                detail={"indicator": selection["indicators"]},
            )
        )

    thresholds = selection["indicators"]
    if len(groups) == 2:
        first, second = groups
        smallest = min(first.total, second.total)
    else:
        smallest = min(item.total for item in groups)
    if smallest < settings.min_group_total:
        warnings.append(
            Issue(
                "small_group_total",
                f"规则 {rule_key!r} 存在样本总数仅 {smallest} 的数据集，"
                f"低于设定下限 {settings.min_group_total}；检验结果的稳定性有限",
                "warning",
                rule=rule_key,
                detail={"min_group_total": smallest,
                        "threshold": settings.min_group_total,
                        "min_expected_cell": thresholds["min_expected_cell"]},
            )
        )

    recommendation = _recommendation(groups, settings)
    result: dict[str, Any] = {
        "rule_key": rule_key,
        "name": name or rule_key,
        "verdict": verdict,
        "interpretation": interpretation,
        "datasets": datasets,
        "dataset_count": len(groups),
        "pooled_hit_rate": recommendation["pooled_hit_rate"],
        "test": test,
        "recommendation": recommendation,
        "evidence": {
            "min_observed_cell": observed_min,
            "min_expected_cell": test["indicators"]["min_expected_cell"],
            "path_marker": "EXACT" if selection["path"] == "exact" else
                            ("NORMAL" if selection["path"] == "normal" else "NONE"),
        },
    }
    return result, warnings


def assess(loaded: LoadedArtifacts, settings: AssessmentSettings) -> dict[str, Any]:
    """Assess all rules and return a deterministic report dictionary."""
    grouped = _group_observations(loaded.observations)
    issues: list[Issue] = list(loaded.issues)
    rules: list[dict[str, Any]] = []

    if not loaded.observations:
        # Nothing usable: return an empty, still-deterministic report skeleton.
        issues_sorted = sorted(issues, key=lambda issue: issue.sort_key)
        return {
            "power_report_version": REPORT_VERSION,
            "settings": settings.to_dict(),
            "inputs": {
                "artifact_count": len(loaded.artifacts),
                "artifacts": [item.to_dict() for item in loaded.artifacts],
                "dataset_count": 0,
                "datasets": [],
                "rule_count": 0,
                "name_aliases": {
                    "datasets": loaded.dataset_aliases,
                    "rules": loaded.rule_aliases,
                },
            },
            "summary": {
                "rule_count": 0,
                "difference": 0,
                "no_significant_difference": 0,
                "no_control": 0,
                "path_exact": 0,
                "path_normal": 0,
                "path_descriptive": 0,
                "error_count": sum(1 for issue in issues_sorted if issue.severity == "error"),
                "warning_count": sum(
                    1 for issue in issues_sorted if issue.severity != "error"
                ),
            },
            "issues": [issue.to_dict() for issue in issues_sorted],
            "rules": [],
        }

    for rule_key, groups in grouped.items():
        rule_result, rule_warnings = _assess_rule(rule_key, groups, settings)
        rules.append(rule_result)
        issues.extend(rule_warnings)

    testable = [item for item in rules if item["verdict"] == "pending"]
    # Across-rule Holm correction applies to every rule that produced a
    # p-value; single-dataset rules are excluded from the family.
    raw_p = [item["test"]["within_rule_adjusted_p"] for item in testable]
    adjusted = (
        holm_adjust(raw_p) if settings.correction == "holm" else list(raw_p)
    )
    for item, value in zip(testable, adjusted):
        p_value = item["test"]["within_rule_adjusted_p"]
        item["test"]["adjusted_p_value"] = value
        significant = value < settings.alpha
        rule_key = item["rule_key"]
        if significant:
            direction = "存在显著差异"
            item["verdict"] = "difference"
        else:
            direction = "未发现显著差异"
            item["verdict"] = "no_significant_difference"
        item["test"]["significant"] = significant
        path_label = "精确检验" if item["test"]["path"] == "exact" else "正态近似"
        item["interpretation"] = (
            f"{direction}：规则 {rule_key!r} 在 {item['dataset_count']} 个数据集上的"
            f"命中率经{path_label}判断 p={p_value:.6g}"
            f"（校正后 {value:.6g}，alpha={settings.alpha:g}）。"
        )
        if not significant and item["recommendation"]["underpowered"]:
            item["interpretation"] += (
                f" 当前每个数据集建议至少 {item['recommendation']['recommended_total_per_dataset']} "
                f"条样本才能可靠检出 {settings.min_effect:g} 的差异；"
                "样本不足时不应把不显著当作“无质量问题”。"
            )

    # Coverage: which loaded datasets participate in each rule.
    all_datasets = sorted({item.dataset for item in loaded.observations})
    for item in rules:
        present = [entry["dataset"] for entry in item["datasets"]]
        absent = [name for name in all_datasets if name not in present]
        item["coverage"] = {
            "compared_datasets": present,
            "absent_datasets": absent,
        }
        if absent:
            issues.append(
                Issue(
                    "rule_dataset_coverage_gap",
                    f"规则 {item['rule_key']!r} 只出现在 {len(present)}/{len(all_datasets)} "
                    f"个数据集中，缺少：{', '.join(absent)}；跨数据集比较仅基于现有数据集",
                    "warning",
                    rule=item["rule_key"],
                )
            )

    verdict_counts = {"difference": 0, "no_significant_difference": 0, "no_control": 0}
    for item in rules:
        verdict_counts[item["verdict"]] = verdict_counts.get(item["verdict"], 0) + 1
    path_counts = {"exact": 0, "normal": 0, "descriptive": 0}
    for item in rules:
        path_counts[item["test"]["path"]] = path_counts.get(item["test"]["path"], 0) + 1

    issues_sorted = sorted(issues, key=lambda issue: issue.sort_key)
    error_count = sum(1 for issue in issues_sorted if issue.severity == "error")
    warning_count = len(issues_sorted) - error_count

    return {
        "power_report_version": REPORT_VERSION,
        "settings": settings.to_dict(),
        "inputs": {
            "artifact_count": len(loaded.artifacts),
            "artifacts": [item.to_dict() for item in loaded.artifacts],
            "dataset_count": len(all_datasets),
            "datasets": all_datasets,
            "rule_count": len(rules),
            "name_aliases": {
                "datasets": loaded.dataset_aliases,
                "rules": loaded.rule_aliases,
            },
        },
        "summary": {
            "rule_count": len(rules),
            **verdict_counts,
            "path_exact": path_counts["exact"],
            "path_normal": path_counts["normal"],
            "path_descriptive": path_counts["descriptive"],
            "error_count": error_count,
            "warning_count": warning_count,
        },
        "issues": [issue.to_dict() for issue in issues_sorted],
        "rules": rules,
    }
