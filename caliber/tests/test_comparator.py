from __future__ import annotations

import pytest

from app.core.comparator import compare_impl_results, values_match
from app.errors import CaliberError


def test_exactly_equal_results_are_consistent():
    result = compare_impl_results(
        {"impl_a": {"2026-01-01": 10, "2026-01-02": 12},
         "impl_b": {"2026-01-01": 10, "2026-01-02": 12}},
        metric_key="m", metric_version=3,
    )
    assert result.verdict == "consistent"
    assert result.diffs == []
    assert result.summary["dimension_count"] == 2
    assert result.summary["consistent_dimensions"] == 2
    assert result.metric_version == 3


def test_absolute_tolerance_boundary():
    inside = compare_impl_results(
        {"a": {"day": 100.0}, "b": {"day": 100.5}},
        abs_tolerance=0.5,
    )
    outside = compare_impl_results(
        {"a": {"day": 100.0}, "b": {"day": 100.5001}},
        abs_tolerance=0.5,
    )
    assert inside.verdict == "consistent"
    assert outside.verdict == "inconsistent"
    assert outside.diffs[0].kind == "value_mismatch"
    assert outside.diffs[0].max_delta == pytest.approx(0.5001)


def test_relative_tolerance_and_combined_formula():
    assert values_match(100.0, 101.0, abs_tolerance=0.0, rel_tolerance=0.01)
    assert not values_match(100.0, 101.1, abs_tolerance=0.0, rel_tolerance=0.01)
    assert values_match(0.0, 0.0005, abs_tolerance=0.0005, rel_tolerance=1.0)
    result = compare_impl_results(
        {"a": {"day": 100.0}, "b": {"day": 100.9}},
        abs_tolerance=0.1, rel_tolerance=0.01,
    )
    assert result.verdict == "consistent"


def test_dimension_missing_yields_partial():
    result = compare_impl_results(
        {
            "impl_a": {"2026-01-01": 3, "2026-01-02": 4},
            "impl_b": {"2026-01-01": 3},
        }
    )
    assert result.verdict == "partial"
    assert result.summary["missing_dimensions"] == 1
    diff = result.diffs[0]
    assert diff.dimension == "2026-01-02"
    assert diff.kind == "missing_dimension"
    assert diff.values["impl_b"] is None
    assert diff.max_delta is None


def test_missing_and_value_mismatch_is_inconsistent():
    result = compare_impl_results(
        {
            "impl_a": {"d1": 1.0, "d2": 5.0},
            "impl_b": {"d1": 2.0},
        }
    )
    assert result.verdict == "inconsistent"
    assert result.summary["value_mismatches"] == 1
    assert result.summary["missing_dimensions"] == 1
    kinds = {diff.dimension: diff.kind for diff in result.diffs}
    assert kinds == {"d1": "value_mismatch", "d2": "missing_dimension"}


def test_alignment_uses_all_dimensions_and_pairwise_comparison():
    result = compare_impl_results(
        {
            "a": {"d1": 1.0, "d2": 2.0},
            "b": {"d1": 1.0, "d2": 2.0, "d3": 3.0},
            "c": {"d1": 1.0, "d2": 2.1, "d3": 3.0},
        },
        abs_tolerance=0.01,
    )
    assert result.dimensions == ["d1", "d2", "d3"]
    assert result.verdict == "inconsistent"
    assert [diff.dimension for diff in result.diffs] == ["d2", "d3"]
    assert result.summary["value_mismatches"] == 1
    assert result.summary["missing_dimensions"] == 1


def test_negative_tolerance_is_rejected():
    with pytest.raises(CaliberError):
        compare_impl_results({"a": {}, "b": {}}, abs_tolerance=-1)
    with pytest.raises(CaliberError):
        compare_impl_results({"a": {}, "b": {}}, rel_tolerance=-0.1)
