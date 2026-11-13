"""Self-contained statistical routines used by the power module.

Only the Python standard library is used so the module stays offline and
deterministic.  Every public function is pure: identical inputs always return
identical outputs.
"""
from __future__ import annotations

import math
from statistics import NormalDist
from typing import Iterable


class ExactBudgetExceeded(RuntimeError):
    """Raised when an exact test would need more enumeration states than allowed."""

    def __init__(self, states: int, budget: int) -> None:
        super().__init__(f"精确检验需要 {states} 个枚举状态，超过预算 {budget}")
        self.states = states
        self.budget = budget


_NORMAL = NormalDist()


def normal_cdf(value: float) -> float:
    return _NORMAL.cdf(value)


def z_quantile(probability: float) -> float:
    if not 0.0 < probability < 1.0:
        raise ValueError("概率必须严格位于 (0, 1) 内")
    return _NORMAL.inv_cdf(probability)


def two_sided_normal_p(z_value: float) -> float:
    probability = 2.0 * (1.0 - normal_cdf(abs(z_value)))
    return min(1.0, max(0.0, probability))


def wilson_interval(hits: int, total: int, confidence: float = 0.95) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion."""
    if total <= 0:
        raise ValueError("样本总数必须大于 0")
    if not 0.0 < confidence < 1.0:
        raise ValueError("置信水平必须严格位于 (0, 1) 内")
    z = z_quantile(1.0 - (1.0 - confidence) / 2.0)
    rate = hits / total
    denominator = 1.0 + z * z / total
    center = (rate + z * z / (2.0 * total)) / denominator
    half = (z / denominator) * math.sqrt(
        rate * (1.0 - rate) / total + z * z / (4.0 * total * total)
    )
    return max(0.0, center - half), min(1.0, center + half)


# ---------------------------------------------------------------------------
# Two-proportion normal approximation
# ---------------------------------------------------------------------------

def two_proportion_z_test(hits_a: int, total_a: int, hits_b: int, total_b: int) -> tuple[float, float]:
    """Pooled two-proportion z test for homogeneity.

    Returns ``(z_statistic, two_sided_p_value)``.  When the pooled standard
    error is zero (both groups have the same boundary rate) the test has no
    discriminating power and returns ``(0.0, 1.0)``.
    """
    if total_a <= 0 or total_b <= 0:
        raise ValueError("样本总数必须大于 0")
    rate_a = hits_a / total_a
    rate_b = hits_b / total_b
    pooled = (hits_a + hits_b) / (total_a + total_b)
    variance = pooled * (1.0 - pooled) * (1.0 / total_a + 1.0 / total_b)
    if variance <= 0.0:
        return 0.0, 1.0
    statistic = (rate_a - rate_b) / math.sqrt(variance)
    return statistic, two_sided_normal_p(statistic)


# ---------------------------------------------------------------------------
# Fisher exact test for a 2 x 2 table
# ---------------------------------------------------------------------------

def fisher_exact_states(hits_a: int, total_a: int, hits_b: int, total_b: int) -> int:
    """Number of hypergeometric states a two-sided Fisher test must walk.

    The table is ``[[hits_a, total_a - hits_a], [hits_b, total_b - hits_b]]``
    with fixed margins; only the top-left cell varies.
    """
    row_a = total_a
    row_b = total_b
    column_hits = hits_a + hits_b
    lower = max(0, column_hits - row_b)
    upper = min(column_hits, row_a)
    return upper - lower + 1


def _log_comb(n: int, k: int) -> float:
    return math.lgamma(n + 1.0) - math.lgamma(k + 1.0) - math.lgamma(n - k + 1.0)


def fisher_exact_two_sided(
    hits_a: int,
    total_a: int,
    hits_b: int,
    total_b: int,
    *,
    state_budget: int = 500_000,
) -> tuple[float, int]:
    """Two-sided Fisher exact p-value for two hit/total groups.

    Returns ``(p_value, enumerated_states)``.  Raises
    :class:`ExactBudgetExceeded` instead of silently switching methods when
    the enumeration would exceed ``state_budget``.
    """
    if total_a <= 0 or total_b <= 0:
        raise ValueError("样本总数必须大于 0")
    if hits_a > total_a or hits_b > total_b:
        raise ValueError("命中数不能超过样本总数")
    states = fisher_exact_states(hits_a, total_a, hits_b, total_b)
    if states > state_budget:
        raise ExactBudgetExceeded(states, state_budget)

    miss_a = total_a - hits_a
    row_a = total_a
    row_b = total_b
    column_hits = hits_a + hits_b
    grand = total_a + total_b
    denominator = _log_comb(grand, row_a)
    lower = max(0, column_hits - row_b)
    upper = min(column_hits, row_a)

    observed = (
        _log_comb(column_hits, hits_a)
        + _log_comb(grand - column_hits, miss_a)
        - denominator
    )
    log_probabilities = []
    for value in range(lower, upper + 1):
        log_probabilities.append(
            _log_comb(column_hits, value)
            + _log_comb(grand - column_hits, row_a - value)
            - denominator
        )
    # Normalize before comparing so extreme tables cannot underflow to zero.
    maximum = max(log_probabilities)
    weights = [math.exp(item - maximum) for item in log_probabilities]
    total_weight = sum(weights)
    observed_weight = math.exp(observed - maximum)
    threshold = observed_weight * (1.0 + 1e-12)
    tail = sum(weight for weight in weights if weight <= threshold)
    p_value = min(1.0, max(0.0, tail / total_weight))
    return p_value, states


# ---------------------------------------------------------------------------
# Chi-square homogeneity test (normal approximation, k >= 2 groups)
# ---------------------------------------------------------------------------

def _regularized_gamma_q(shape: float, value: float) -> float:
    """Upper regularized incomplete gamma Q(shape, value) (continued fraction)."""
    tiny = 1e-300
    b = value + 1.0 - shape
    c = 1.0 / tiny
    d = 1.0 / max(b, tiny)
    h = d
    for index in range(1, 1000):
        an = -index * (index - shape)
        b += 2.0
        d = an * d + b
        if abs(d) < tiny:
            d = tiny
        c = b + an / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-15:
            break
    return math.exp(-value + shape * math.log(value) - math.lgamma(shape)) * h


def chi_square_survival(statistic: float, degrees_of_freedom: int) -> float:
    """Upper-tail p-value of a chi-square distribution."""
    if degrees_of_freedom <= 0:
        raise ValueError("自由度必须大于 0")
    if statistic <= 0.0:
        return 1.0
    return min(1.0, max(0.0, _regularized_gamma_q(degrees_of_freedom / 2.0, statistic / 2.0)))


def chi_square_homogeneity(counts: Iterable[tuple[int, int]]) -> tuple[float, int, float]:
    """Chi-square homogeneity test over ``(hits, total)`` groups.

    Returns ``(statistic, degrees_of_freedom, p_value)``.  Degrees of freedom
    are ``groups - 1``; the non-hit column is implied by ``total - hits``.
    """
    groups = [(int(hits), int(total)) for hits, total in counts]
    if len(groups) < 2:
        raise ValueError("卡方齐性检验至少需要两个数据集")
    for hits, total in groups:
        if total <= 0 or hits < 0 or hits > total:
            raise ValueError("命中数与样本总数不合法")
    grand_total = sum(total for _, total in groups)
    total_hits = sum(hits for hits, _ in groups)
    statistic = 0.0
    for hits, total in groups:
        expected_hits = total_hits * total / grand_total
        expected_misses = (grand_total - total_hits) * total / grand_total
        if expected_hits <= 0.0 or expected_misses <= 0.0:
            raise ValueError("存在零预期格，不能使用卡方近似")
        statistic += (hits - expected_hits) ** 2 / expected_hits
        misses = total - hits
        statistic += (misses - expected_misses) ** 2 / expected_misses
    degrees = len(groups) - 1
    return statistic, degrees, chi_square_survival(statistic, degrees)


def expected_cell_minimum(counts: Iterable[tuple[int, int]]) -> float:
    """Smallest expected cell count under the pooled hit rate."""
    groups = [(int(hits), int(total)) for hits, total in counts]
    grand_total = sum(total for _, total in groups)
    if grand_total <= 0:
        return 0.0
    pooled = sum(hits for hits, _ in groups) / grand_total
    values = []
    for _, total in groups:
        values.extend((total * pooled, total * (1.0 - pooled)))
    return min(values) if values else 0.0


def min_sparse_cell(counts: Iterable[tuple[int, int]]) -> int:
    """Smallest of hits / non-hits across groups (the sparse-corner statistic)."""
    values = []
    for hits, total in counts:
        values.extend((int(hits), int(total) - int(hits)))
    return min(values) if values else 0


# ---------------------------------------------------------------------------
# Multiple testing
# ---------------------------------------------------------------------------

def holm_adjust(p_values: Iterable[float]) -> list[float]:
    """Holm step-down adjusted p-values, order-preserving."""
    values = [min(1.0, max(0.0, float(item))) for item in p_values]
    if not values:
        return []
    order = sorted(range(len(values)), key=lambda index: (values[index], index))
    adjusted = [1.0] * len(values)
    running = 0.0
    total = len(values)
    for rank, index in enumerate(order):
        candidate = min(1.0, values[index] * (total - rank))
        running = max(running, candidate)
        adjusted[index] = running
    return adjusted


# ---------------------------------------------------------------------------
# Sample-size planning
# ---------------------------------------------------------------------------

def _ceil(value: float) -> int:
    # Guard against 100.00000000000001 style artefacts of float arithmetic.
    return int(math.ceil(value - 1e-9))


def sample_size_for_effect(
    baseline_rate: float,
    *,
    alpha: float = 0.05,
    power: float = 0.80,
    min_effect: float = 0.05,
    min_expected_cell: float = 5.0,
    minimum_total: int = 30,
) -> dict[str, object]:
    """Per-group sample size needed to detect ``min_effect`` around a baseline.

    Uses the standard two-proportion normal-approximation power formula and
    augments it with the expected-cell requirement.  The returned dictionary
    is fully explainable so the recommendation can be audited.
    """
    if not 0.0 <= baseline_rate <= 1.0:
        raise ValueError("基准命中率必须位于 [0, 1] 内")
    if not 0.0 < alpha < 1.0 or not 0.0 < power < 1.0:
        raise ValueError("alpha 与 power 必须严格位于 (0, 1) 内")
    if not 0.0 < min_effect <= 1.0:
        raise ValueError("最小可检测差异必须位于 (0, 1] 内")

    z_alpha = z_quantile(1.0 - alpha / 2.0)
    z_power = z_quantile(power)
    baseline = baseline_rate
    if baseline + min_effect <= 1.0:
        target = baseline + min_effect
    elif baseline - min_effect >= 0.0:
        target = baseline - min_effect
    else:
        target = baseline + min_effect  # unreachable for min_effect <= 1
    target = min(1.0, max(0.0, target))
    delta = abs(target - baseline)
    if delta <= 0.0:
        normal_n: int | None = None
    else:
        variance = baseline * (1.0 - baseline) + target * (1.0 - target)
        normal_n = _ceil((z_alpha + z_power) ** 2 * variance / (delta * delta))

    if 0.0 < baseline < 1.0:
        cell_n: int | None = _ceil(min_expected_cell / min(baseline, 1.0 - baseline))
    else:
        cell_n = None

    candidates = [value for value in (normal_n, cell_n, minimum_total) if value is not None]
    recommended = max(candidates)
    return {
        "baseline_rate": baseline,
        "target_rate": target,
        "effect": delta,
        "alpha": alpha,
        "power": power,
        "z_alpha": z_alpha,
        "z_power": z_power,
        "normal_power_sample_size": normal_n,
        "expected_cell_sample_size": cell_n,
        "minimum_total_floor": minimum_total,
        "recommended_total_per_dataset": recommended,
        "assumptions": (
            "两个等样本量数据集、双侧检验、命中率由基准率向可检测方向偏移；"
            "正态近似功效公式，精确路径所需的实际样本可能更大。"
        ),
    }
