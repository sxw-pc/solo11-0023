from __future__ import annotations

import pytest

from app.power.stats import (
    ExactBudgetExceeded,
    chi_square_homogeneity,
    chi_square_survival,
    expected_cell_minimum,
    fisher_exact_states,
    fisher_exact_two_sided,
    holm_adjust,
    min_sparse_cell,
    sample_size_for_effect,
    two_proportion_z_test,
    wilson_interval,
)


def test_fisher_exact_matches_known_table():
    # Classic tea-tasting style table; two-sided p = 0.0697785...
    p_value, states = fisher_exact_two_sided(3, 10, 8, 10)
    assert p_value == pytest.approx(0.06977851869492757, rel=1e-9)
    assert states == 10


def test_fisher_exact_identical_groups_gives_p_one():
    p_value, states = fisher_exact_two_sided(5, 10, 5, 10)
    assert p_value == pytest.approx(1.0)
    # Enumerated support for the fixed margins is hits_a in 0..10.
    assert states == 11


def test_fisher_exact_rejects_state_overflow():
    states = fisher_exact_states(100, 500_000, 120, 500_000)
    with pytest.raises(ExactBudgetExceeded):
        fisher_exact_two_sided(100, 500_000, 120, 500_000, state_budget=100)
    assert states > 100


def test_chi_square_survival_matches_critical_values():
    assert chi_square_survival(3.841, 1) == pytest.approx(0.05, abs=5e-4)
    assert chi_square_survival(5.991, 2) == pytest.approx(0.05, abs=5e-4)
    assert chi_square_survival(7.815, 3) == pytest.approx(0.05, abs=5e-4)
    assert chi_square_survival(0.0, 3) == 1.0


def test_chi_square_homogeneity_detects_large_difference():
    statistic, degrees, p_value = chi_square_homogeneity([(500, 1000), (650, 1000)])
    assert degrees == 1
    assert statistic > 0
    assert p_value < 1e-6


def test_z_test_and_chi_square_agree_for_two_groups():
    z_statistic, z_p = two_proportion_z_test(500, 1000, 650, 1000)
    statistic, _, chi_p = chi_square_homogeneity([(500, 1000), (650, 1000)])
    assert z_statistic ** 2 == pytest.approx(statistic, rel=1e-9)
    assert z_p == pytest.approx(chi_p, rel=1e-6)


def test_z_test_boundary_rates_are_not_significant():
    assert two_proportion_z_test(0, 100, 0, 100) == (0.0, 1.0)
    assert two_proportion_z_test(100, 100, 100, 100) == (0.0, 1.0)


def test_wilson_interval_is_bounded_and_narrows():
    low, high = wilson_interval(0, 10)
    assert 0.0 <= low < high < 1.0
    narrow_low, narrow_high = wilson_interval(0, 1000)
    assert narrow_high < high


def test_expected_and_observed_sparse_cells():
    counts = [(0, 1000), (3, 1000)]
    assert expected_cell_minimum(counts) == pytest.approx(1.5)
    assert min_sparse_cell(counts) == 0


def test_holm_adjustment_is_monotone_and_order_preserving():
    adjusted = holm_adjust([0.01, 0.04, 0.03])
    assert adjusted == pytest.approx([0.03, 0.06, 0.06])
    assert holm_adjust([]) == []


def test_sample_size_grows_with_smaller_effect():
    small = sample_size_for_effect(0.5, min_effect=0.10)["recommended_total_per_dataset"]
    large = sample_size_for_effect(0.5, min_effect=0.02)["recommended_total_per_dataset"]
    assert large > small


def test_sample_size_respects_expected_cell_floor_for_rare_rate():
    plan = sample_size_for_effect(0.01, min_effect=0.02, min_expected_cell=5)
    assert plan["expected_cell_sample_size"] == 500
    assert plan["recommended_total_per_dataset"] >= 500
    assert "假设" in plan["assumptions"] or plan["assumptions"]
