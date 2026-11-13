from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.power import PowerInputError, load_artifacts, render
from app.power.engine import assess
from app.power.errors import PowerConfigError
from app.power.models import AssessmentSettings

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "power"


def settings(**overrides) -> AssessmentSettings:
    return AssessmentSettings(**overrides)


def rules_by_key(report):
    return {item["rule_key"]: item for item in report["rules"]}


# ---------------------------------------------------------------------------
# Acceptance: known real differences vs known pure noise
# ---------------------------------------------------------------------------

def test_real_differences_and_noise_are_separated():
    loaded = load_artifacts(
        [
            FIXTURES / "real_diff_a.json",
            FIXTURES / "real_diff_b.json",
            FIXTURES / "noise_a.json",
            FIXTURES / "noise_b.json",
        ]
    )
    assert not loaded.errors
    report = assess(loaded, settings())
    rules = rules_by_key(report)
    assert rules["amount_nonnegative"]["verdict"] == "difference"
    assert rules["channel_known"]["verdict"] == "difference"
    assert rules["refund_flag_ratio"]["verdict"] == "no_significant_difference"
    assert rules["discount_flag_ratio"]["verdict"] == "no_significant_difference"
    assert report["summary"]["difference"] == 2
    assert report["summary"]["no_significant_difference"] == 2


def test_noise_rules_have_large_p_values():
    loaded = load_artifacts([FIXTURES / "noise_a.json", FIXTURES / "noise_b.json"])
    report = assess(loaded, settings())
    for rule in report["rules"]:
        assert rule["test"]["adjusted_p_value"] > 0.5


# ---------------------------------------------------------------------------
# Acceptance: sparse rules take the exact path and mark it
# ---------------------------------------------------------------------------

def test_sparse_rule_selects_exact_path_with_marker_and_reason():
    loaded = load_artifacts([FIXTURES / "sparse_exact.json"])
    report = assess(loaded, settings())
    rules = rules_by_key(report)
    for key in ("refund_null_ratio", "coupon_orphan_ratio"):
        rule = rules[key]
        assert rule["test"]["path"] == "exact"
        assert rule["test"]["method"] == "fisher_exact"
        assert rule["evidence"]["path_marker"] == "EXACT"
        assert rule["test"]["reason_code"] == "sparse_cells"
        assert "精确检验" in rule["test"]["reason"]
        assert rule["test"]["indicators"]["min_expected_cell"] < 5
        assert rule["test"]["pairwise"][0]["enumerated_states"] > 0
    # The exhaustive state budget and the reason are auditable in the result.
    assert report["summary"]["path_exact"] == 2
    marker = render(report, "md")
    assert "精确检验" in marker and "标记 `EXACT`" in marker


def test_exact_path_is_downgraded_with_explicit_reason_when_budget_too_small():
    loaded = load_artifacts([FIXTURES / "sparse_exact.json"])
    report = assess(loaded, settings(exact_state_budget=2))
    rule = rules_by_key(report)["refund_null_ratio"]
    assert rule["test"]["path"] == "normal"
    assert rule["test"]["degraded"] is True
    assert rule["test"]["degradation"] == "exact_budget_exceeded"
    assert "降级" in rule["test"]["reason"]
    assert any(issue["code"] == "exact_downgraded" for issue in report["issues"])


# ---------------------------------------------------------------------------
# Acceptance: single-dataset artifact is marked "no control"
# ---------------------------------------------------------------------------

def test_single_dataset_is_marked_no_control():
    loaded = load_artifacts([FIXTURES / "single_dataset.json"])
    report = assess(loaded, settings())
    rule = report["rules"][0]
    assert rule["verdict"] == "no_control"
    assert rule["test"]["path"] == "descriptive"
    assert rule["test"]["p_value"] is None
    assert rule["evidence"]["path_marker"] == "NONE"
    assert report["summary"]["no_control"] == 1
    assert "无对照" in rule["interpretation"]
    assert "无对照" in render(report, "md")


# ---------------------------------------------------------------------------
# Acceptance: missing fields produce readable errors, never silent skips
# ---------------------------------------------------------------------------

def test_missing_field_reports_readable_error():
    path = FIXTURES / "invalid" / "missing_total.json"
    loaded = load_artifacts([path])
    assert loaded.errors
    error = loaded.errors[0]
    assert error.code == "missing_fields"
    assert "缺少必填字段" in error.message
    assert "total" in error.message
    assert "第 1 条记录" in error.message


def test_missing_artifact_path_is_reported(tmp_path: Path):
    loaded = load_artifacts([tmp_path / "does_not_exist.json"])
    assert [issue.code for issue in loaded.errors] == ["artifact_not_found"]


def test_malformed_json_is_reported(tmp_path: Path):
    broken = tmp_path / "broken.json"
    broken.write_text("{not valid json", encoding="utf-8")
    loaded = load_artifacts([broken])
    assert [issue.code for issue in loaded.errors] == ["artifact_parse_error"]


def test_unsupported_format_is_reported(tmp_path: Path):
    other = tmp_path / "artifact.yaml"
    other.write_text("rules: []", encoding="utf-8")
    loaded = load_artifacts([other])
    assert [issue.code for issue in loaded.errors] == ["unsupported_format"]


def test_invalid_counts_are_reported():
    loaded = load_artifacts([FIXTURES / "invalid" / "missing_total.json"])
    assert loaded.observations == []
    assert all(issue.severity == "error" for issue in loaded.errors)


def test_unknown_field_is_a_warning_not_silent_drop(tmp_path: Path):
    artifact = tmp_path / "extra.json"
    artifact.write_text(
        json.dumps(
            {
                "rules": [
                    {
                        "rule_key": "r",
                        "datasets": [
                            {"dataset": "a", "hits": 1, "passes": 9, "total": 10,
                             "hit_rate": 0.1}
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    loaded = load_artifacts([artifact])
    assert not loaded.errors
    warning = next(item for item in loaded.warnings if item.code == "unknown_field")
    assert "hit_rate" in warning.message


# ---------------------------------------------------------------------------
# Acceptance: reproducibility / determinism
# ---------------------------------------------------------------------------

def test_repeated_runs_are_byte_identical():
    paths = [
        FIXTURES / "real_diff_a.json",
        FIXTURES / "real_diff_b.json",
        FIXTURES / "noise_a.json",
        FIXTURES / "noise_b.json",
        FIXTURES / "sparse_exact.json",
    ]
    first = render(assess(load_artifacts(paths), settings()), "json")
    second = render(assess(load_artifacts(paths), settings()), "json")
    assert first == second
    assert '"generated_at"' not in first


def test_artifact_order_does_not_change_result():
    paths = [
        FIXTURES / "real_diff_a.json",
        FIXTURES / "real_diff_b.json",
        FIXTURES / "noise_a.json",
    ]
    forward = render(assess(load_artifacts(paths), settings()), "json")
    backward = render(assess(load_artifacts(list(reversed(paths))), settings()), "json")
    assert forward == backward


# ---------------------------------------------------------------------------
# Cross-artifact aggregation, naming and coverage
# ---------------------------------------------------------------------------

def test_name_variants_are_merged_with_explicit_warning(tmp_path: Path):
    artifact = tmp_path / "aliases.json"
    artifact.write_text(
        json.dumps(
            {
                "records": [
                    {"rule": "amount_check", "dataset": "Orders_A",
                     "hit_count": "40", "pass_count": "60", "sample_total": 100},
                    {"rule_key": "amount_check", "dataset": "orders_a",
                     "hits": 40, "passes": 60, "total": 100},
                    {"rule_key": "amount_check", "dataset": "orders-b",
                     "hits": 55, "passes": 45, "total": 100},
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    loaded = load_artifacts([artifact])
    assert not loaded.errors
    assert {item.dataset for item in loaded.observations} == {"orders_a", "orders_b"}
    codes = {issue.code for issue in loaded.warnings}
    assert "dataset_name_normalized" in codes
    assert "duplicate_observation" in codes


def test_conflicting_duplicate_is_an_error(tmp_path: Path):
    artifact = tmp_path / "conflict.json"
    artifact.write_text(
        json.dumps(
            {
                "records": [
                    {"rule_key": "r", "dataset": "a", "hits": 1, "passes": 9, "total": 10},
                    {"rule_key": "r", "dataset": "a", "hits": 2, "passes": 8, "total": 10},
                ]
            }
        ),
        encoding="utf-8",
    )
    loaded = load_artifacts([artifact])
    assert any(issue.code == "conflicting_observation" for issue in loaded.errors)


def test_rule_absent_from_some_datasets_emits_coverage_warning(tmp_path: Path):
    artifact = tmp_path / "partial.json"
    artifact.write_text(
        json.dumps(
            {
                "rules": [
                    {
                        "rule_key": "partial_rule",
                        "datasets": [
                            {"dataset": "a", "hits": 50, "passes": 50, "total": 100},
                            {"dataset": "b", "hits": 52, "passes": 48, "total": 100},
                        ],
                    },
                    {
                        "rule_key": "other_rule",
                        "datasets": [
                            {"dataset": "a", "hits": 10, "passes": 90, "total": 100}
                        ],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    report = assess(load_artifacts([artifact]), settings())
    rule = rules_by_key(report)["partial_rule"]
    assert rule["coverage"]["absent_datasets"] == []
    other = rules_by_key(report)["other_rule"]
    assert other["coverage"]["absent_datasets"] == ["b"]
    assert any(
        issue["code"] == "rule_dataset_coverage_gap" for issue in report["issues"]
    )


def test_jsonl_artifact_is_supported(tmp_path: Path):
    artifact = tmp_path / "records.jsonl"
    lines = [
        json.dumps({"rule_key": "r", "dataset": "a", "hits": 50, "passes": 50, "total": 100}),
        json.dumps({"rule_key": "r", "dataset": "b", "hits": 60, "passes": 40, "total": 100}),
    ]
    artifact.write_text("\n".join(lines) + "\n", encoding="utf-8")
    report = assess(load_artifacts([artifact]), settings())
    assert report["inputs"]["rule_count"] == 1
    assert report["rules"][0]["test"]["path"] == "normal"


def test_jsonl_malformed_line_is_reported(tmp_path: Path):
    artifact = tmp_path / "broken.jsonl"
    artifact.write_text('{"rule_key": "r"\n', encoding="utf-8")
    loaded = load_artifacts([artifact])
    assert loaded.errors[0].code == "artifact_parse_error"
    assert "第 1 行" in loaded.errors[0].message


def test_non_integer_count_is_reported(tmp_path: Path):
    artifact = tmp_path / "float.json"
    artifact.write_text(
        json.dumps({"records": [{"rule_key": "r", "dataset": "a",
                                 "hits": 1.5, "passes": 9, "total": 10}]}),
        encoding="utf-8",
    )
    loaded = load_artifacts([artifact])
    assert loaded.errors[0].code == "invalid_count"
    assert "整数" in loaded.errors[0].message


def test_zero_total_is_reported(tmp_path: Path):
    artifact = tmp_path / "zero.json"
    artifact.write_text(
        json.dumps({"records": [{"rule_key": "r", "dataset": "a",
                                 "hits": 0, "passes": 0, "total": 0}]}),
        encoding="utf-8",
    )
    loaded = load_artifacts([artifact])
    assert loaded.errors[0].code == "invalid_count"


def test_unsupported_artifact_version_is_reported(tmp_path: Path):
    artifact = tmp_path / "future.json"
    artifact.write_text(
        json.dumps({"artifact_version": "2.0", "rules": []}), encoding="utf-8"
    )
    loaded = load_artifacts([artifact])
    assert loaded.errors[0].code == "unsupported_artifact_version"


def test_root_list_artifact_is_supported(tmp_path: Path):
    artifact = tmp_path / "list.json"
    artifact.write_text(
        json.dumps(
            [
                {"rule_key": "r", "dataset": "a", "hits": 50, "passes": 50, "total": 100},
                {"rule_key": "r", "dataset": "b", "hits": 52, "passes": 48, "total": 100},
            ]
        ),
        encoding="utf-8",
    )
    report = assess(load_artifacts([artifact]), settings())
    assert report["inputs"]["rule_count"] == 1


def test_artifact_with_no_records_is_warned(tmp_path: Path):
    artifact = tmp_path / "empty.json"
    artifact.write_text(json.dumps({"rules": []}), encoding="utf-8")
    loaded = load_artifacts([artifact])
    assert not loaded.errors
    assert [issue.code for issue in loaded.warnings] == ["artifact_no_records"]


# ---------------------------------------------------------------------------
# Statistical decisions and planning
# ---------------------------------------------------------------------------

def test_multi_dataset_boundary_rule_is_downgraded_explicitly(tmp_path: Path):
    artifact = tmp_path / "huge.json"
    records = [{"rule_key": "r", "dataset": "zero", "hits": 0,
                "passes": 1_000_000, "total": 1_000_000}]
    for index in range(4):
        records.append(
            {"rule_key": "r", "dataset": f"g{index}", "hits": 400_000 + index * 1000,
             "passes": 600_000, "total": 1_000_000}
        )
    artifact.write_text(json.dumps({"records": records}), encoding="utf-8")
    report = assess(load_artifacts([artifact]), settings())
    rule = report["rules"][0]
    assert rule["test"]["path"] == "normal"
    assert rule["test"]["degraded"] is True
    assert rule["test"]["degradation"] == "exact_budget_exceeded"
    assert "超过预算" in rule["test"]["reason"]
    assert any(issue["code"] == "exact_downgraded" for issue in report["issues"])


def test_multi_dataset_exact_path_uses_pairwise_holm(tmp_path: Path):
    artifact = tmp_path / "sparse3.json"
    artifact.write_text(
        json.dumps(
            {
                "records": [
                    {"rule_key": "sparse3", "dataset": "d1", "hits": 0, "passes": 500, "total": 500},
                    {"rule_key": "sparse3", "dataset": "d2", "hits": 1, "passes": 499, "total": 500},
                    {"rule_key": "sparse3", "dataset": "d3", "hits": 2, "passes": 498, "total": 500},
                ]
            }
        ),
        encoding="utf-8",
    )
    report = assess(load_artifacts([artifact]), settings())
    rule = report["rules"][0]
    assert rule["test"]["path"] == "exact"
    assert rule["test"]["method"] == "fisher_exact_pairwise"
    assert len(rule["test"]["pairwise"]) == 3
    for pair in rule["test"]["pairwise"]:
        assert pair["adjusted_p_value"] >= pair["p_value"]
        assert pair["enumerated_states"] > 0


def test_all_boundary_rule_gets_pooled_warning(tmp_path: Path):
    artifact = tmp_path / "all_zero.json"
    artifact.write_text(
        json.dumps(
            {
                "records": [
                    {"rule_key": "never_hit", "dataset": "a", "hits": 0, "passes": 100, "total": 100},
                    {"rule_key": "never_hit", "dataset": "b", "hits": 0, "passes": 100, "total": 100},
                ]
            }
        ),
        encoding="utf-8",
    )
    report = assess(load_artifacts([artifact]), settings())
    codes = {issue["code"] for issue in report["issues"]}
    assert "pooled_boundary_rate" in codes
    for dataset in report["rules"][0]["recommendation"]["per_dataset"]:
        assert dataset["baseline_source"] == "pooled_fallback"


def test_small_samples_do_not_claim_significance_and_recommend_more(tmp_path: Path):
    artifact = tmp_path / "tiny.json"
    artifact.write_text(
        json.dumps(
            {
                "records": [
                    {"rule_key": "tiny_rule", "dataset": "a", "hits": 5, "passes": 5, "total": 10},
                    {"rule_key": "tiny_rule", "dataset": "b", "hits": 7, "passes": 3, "total": 10},
                ]
            }
        ),
        encoding="utf-8",
    )
    report = assess(load_artifacts([artifact]), settings())
    rule = report["rules"][0]
    assert rule["verdict"] == "no_significant_difference"
    assert rule["recommendation"]["underpowered"] is True
    assert rule["recommendation"]["recommended_total_per_dataset"] > 10
    assert "样本不足" in rule["interpretation"]


def test_holm_correction_is_applied_across_rules():
    loaded = load_artifacts([FIXTURES / "real_diff_a.json", FIXTURES / "real_diff_b.json"])
    uncorrected = assess(load_artifacts([FIXTURES / "real_diff_a.json", FIXTURES / "real_diff_b.json"]),
                         settings(correction="none"))
    corrected = assess(loaded, settings(correction="holm"))
    for before, after in zip(
        sorted(uncorrected["rules"], key=lambda item: item["rule_key"]),
        sorted(corrected["rules"], key=lambda item: item["rule_key"]),
    ):
        assert after["test"]["adjusted_p_value"] >= before["test"]["p_value"]


def test_conflicting_statistical_settings_are_rejected():
    with pytest.raises(ValueError):
        AssessmentSettings(alpha=1.5)
    with pytest.raises(ValueError):
        AssessmentSettings(min_effect=0)
    with pytest.raises(ValueError):
        AssessmentSettings(correction="bonferroni")


def test_unsupported_output_format_raises():
    loaded = load_artifacts([FIXTURES / "single_dataset.json"])
    report = assess(loaded, settings())
    with pytest.raises(PowerConfigError):
        render(report, "csv")


def test_no_paths_is_input_error():
    with pytest.raises(PowerInputError):
        load_artifacts([])
