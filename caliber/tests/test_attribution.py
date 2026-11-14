"""Tests for streaming natural-key alignment and difference clustering."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core import attribution
from app.core.attribution import (
    DIFF_ONLY_ONE_SIDE,
    DIFF_TYPE_MISMATCH,
    DIFF_VALUE_DEVIATION,
    attribute_differences,
    degraded_from_error,
    skipped_attribution,
)


def write_detail(path: Path, columns: list[str], rows: list[tuple]) -> str:
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(columns) + "\n")
        for row in rows:
            handle.write(json.dumps(dict(zip(columns, row))) + "\n")
    return str(path)


@pytest.fixture
def detail_pair(tmp_path: Path):
    def _build(rows_a, rows_b, columns=("region", "segment", "revenue")):
        file_a = write_detail(tmp_path / "a.jsonl", list(columns), rows_a)
        file_b = write_detail(tmp_path / "b.jsonl", list(columns), rows_b)
        return {"impl_a": file_a, "impl_b": file_b}

    return _build


# --- 验收 1：三个差异簇、影响键数、代表键与人工标注一致 ----------------------

def test_three_clusters_match_manual_annotation(detail_pair):
    rows_a = [
        ("east", "x1", 100),    # 仅 A 存在
        ("east", "x2", 200),    # 数值偏离 5%
        ("east", "x3", 300),    # 数值偏离 10%
        ("east", "x4", 400),    # 数值偏离 10%
        ("north", "m1", 1000),  # B 是文本 -> 类型不一致
        ("north", "m2", 2000),  # B 是 NULL -> 类型不一致
        ("north", "m3", 3000),  # 数值偏离 1%
        ("north", "m4", 4000),  # 数值偏离 1%
        ("south", "s1", 50),    # 完全一致
        ("south", "s2", 80),    # 完全一致
    ]
    rows_b = [
        ("east", "x2", 210),
        ("east", "x3", 330),
        ("east", "x4", 440),
        ("north", "m1", "n/a"),
        ("north", "m2", "missing"),
        ("north", "m3", 3150),
        ("north", "m4", 4200),
        ("south", "s1", 50),
        ("south", "s2", 80),
    ]
    result = attribute_differences(
        detail_pair(rows_a, rows_b),
        ["region", "segment"],
        abs_tolerance=1e-6,
        rel_tolerance=0.0,
        chunk_size=3,  # 强制多分区，验证分块路径
        max_representatives=5,
    )
    assert result["status"] == "ok"
    assert result["total_aligned_keys"] == 10
    assert result["affected_keys"] == 8
    assert len(result["clusters"]) == 3
    by_type = {cluster["diff_type"]: cluster for cluster in result["clusters"]}

    only_side = by_type[DIFF_ONLY_ONE_SIDE]
    assert only_side["affected_keys"] == 1
    assert only_side["representative_keys"] == [["east", "x1"]]
    assert only_side["representative_rows"][0]["values"] == {
        "impl_a": 100,
        "impl_b": None,
    }

    type_mismatch = by_type[DIFF_TYPE_MISMATCH]
    assert type_mismatch["affected_keys"] == 2
    assert type_mismatch["representative_keys"] == [
        ["north", "m1"],
        ["north", "m2"],
    ]
    assert type_mismatch["signature"] == "types:impl_a:integer|impl_b:string"

    deviation = by_type[DIFF_VALUE_DEVIATION]
    assert deviation["affected_keys"] == 5
    # 所有偏离都在 (1%, 10%] 同一分箱，因此归为一个簇
    assert deviation["signature"] == "band:(1%, 10%]"
    assert len(deviation["representative_keys"]) == 5
    # 占比之和受影响键数一致
    assert sum(cluster["affected_keys"] for cluster in result["clusters"]) == 8
    assert result["meta"]["spilled_to_disk"] is True
    assert result["meta"]["partition_count"] >= 2


# --- 验收 2：连跑两遍簇划分稳定、代表键有界 -------------------------------

def test_clustering_is_stable_across_runs(detail_pair):
    rows_a = [(f"r{i % 4}", f"s{i}", i * 100) for i in range(40)]
    rows_b = [
        (f"r{i % 4}", f"s{i}", i * 100 + i)  # 小比例偏离
        for i in range(40)
    ]
    files = detail_pair(rows_a, rows_b)
    first = attribute_differences(
        files, ["region", "segment"], abs_tolerance=0.0, rel_tolerance=0.0,
        chunk_size=7, max_representatives=3,
    )
    second = attribute_differences(
        files, ["region", "segment"], abs_tolerance=0.0, rel_tolerance=0.0,
        chunk_size=11, max_representatives=3,
    )
    # i=0 时两侧都是 0，无差异，其余 39 键落在同一偏离分箱
    assert first["clusters"][0]["affected_keys"] == 39
    assert [c["diff_type"] for c in first["clusters"]] == [
        c["diff_type"] for c in second["clusters"]
    ]
    assert [c["signature"] for c in first["clusters"]] == [
        c["signature"] for c in second["clusters"]
    ]
    assert [c["affected_keys"] for c in first["clusters"]] == [
        c["affected_keys"] for c in second["clusters"]
    ]
    assert [c["representative_keys"] for c in first["clusters"]] == [
        c["representative_keys"] for c in second["clusters"]
    ]


def test_representatives_are_bounded_even_with_many_keys(detail_pair, tmp_path):
    rows_a = [("east", f"k{i:04d}", 100) for i in range(500)]
    rows_b = [("east", f"k{i:04d}", 200) for i in range(500)]
    result = attribute_differences(
        detail_pair(rows_a, rows_b), ["region", "segment"],
        abs_tolerance=0.0, rel_tolerance=0.0,
        chunk_size=64, max_representatives=5, spill_root=str(tmp_path),
    )
    assert len(result["clusters"]) == 1
    cluster = result["clusters"][0]
    assert cluster["affected_keys"] == 500
    assert len(cluster["representative_keys"]) == 5
    assert len(cluster["representative_rows"]) == 5
    # 代表键是确定性的字典序前 5 个
    assert cluster["representative_keys"] == [
        ["east", f"k{i:04d}"] for i in range(5)
    ]
    # 磁盘暂存目录在归因结束后被清理
    assert not list(Path(tmp_path).glob("caliber-attr-*"))


def test_matching_keys_form_no_cluster(detail_pair):
    rows = [("east", "x1", 1.0), ("east", "x2", 2.0)]
    result = attribute_differences(
        detail_pair(rows, rows), ["region", "segment"],
        abs_tolerance=1e-6, rel_tolerance=0.0,
    )
    assert result["status"] == "ok"
    assert result["clusters"] == []
    assert result["affected_keys"] == 0
    assert result["affected_ratio"] == 0.0


def test_missing_key_column_degrades_without_raising(tmp_path):
    file_a = write_detail(
        tmp_path / "a.jsonl", ["region", "revenue"], [("east", 1.0)]
    )
    file_b = write_detail(
        tmp_path / "b.jsonl", ["region", "revenue"], [("east", 1.1)]
    )
    result = attribute_differences(
        {"impl_a": file_a, "impl_b": file_b},
        ["region", "segment"],
        abs_tolerance=0.0, rel_tolerance=0.0,
    )
    assert result["status"] == "degraded"
    assert result["degraded_reasons"][0]["code"] == "natural_key_column_missing"
    assert result["clusters"] == []
    assert result["total_aligned_keys"] == 0


def test_duplicate_natural_keys_are_counted_and_first_row_wins(detail_pair):
    rows_a = [("east", "x1", 100), ("east", "x1", 999)]
    rows_b = [("east", "x1", 100)]
    result = attribute_differences(
        detail_pair(rows_a, rows_b), ["region", "segment"],
        abs_tolerance=1e-6, rel_tolerance=0.0,
    )
    assert result["meta"]["duplicate_natural_keys"]["impl_a"] == 1
    assert result["clusters"] == []  # 首行与 B 一致


def test_skipped_and_error_blocks_are_structured():
    skipped = skipped_attribution()
    assert skipped["status"] == "skipped"
    assert skipped["degraded_reasons"][0]["code"] == "natural_key_not_declared"
    assert skipped["key_strategy"] == "dsl_declared"
    assert skipped["fallback_policy"]

    degraded = degraded_from_error(
        ["day"], ["impl_a", "impl_b"], RuntimeError("boom")
    )
    assert degraded["status"] == "degraded"
    assert degraded["degraded_reasons"][0]["code"] == "attribution_error"
    assert "boom" in degraded["degraded_reasons"][0]["error_message"]


def test_relative_tolerance_is_honored(detail_pair):
    rows_a = [("east", "x1", 100.0)]
    rows_b = [("east", "x1", 100.5)]
    inside = attribute_differences(
        detail_pair(rows_a, rows_b), ["region", "segment"],
        abs_tolerance=0.0, rel_tolerance=0.01,
    )
    assert inside["clusters"] == []
    outside = attribute_differences(
        detail_pair(rows_a, rows_b), ["region", "segment"],
        abs_tolerance=0.0, rel_tolerance=0.001,
    )
    assert outside["clusters"][0]["diff_type"] == DIFF_VALUE_DEVIATION


def test_presence_clusters_split_by_side(detail_pair):
    rows_a = [("east", "a_only", 1.0), ("east", "shared", 2.0)]
    rows_b = [("east", "b_only", 3.0), ("east", "shared", 2.0)]
    result = attribute_differences(
        detail_pair(rows_a, rows_b), ["region", "segment"],
        abs_tolerance=0.0, rel_tolerance=0.0,
    )
    signatures = sorted(c["signature"] for c in result["clusters"])
    assert signatures == ["present:impl_a", "present:impl_b"]
    assert all(c["diff_type"] == DIFF_ONLY_ONE_SIDE for c in result["clusters"])


def test_single_column_natural_key_and_partition_count(detail_pair):
    rows_a = [("2026-02-01", 1.0), ("2026-02-02", 2.0)]
    rows_b = [("2026-02-01", 1.0), ("2026-02-02", 9.0)]
    result = attribute_differences(
        detail_pair(rows_a, rows_b, columns=("day", "value")),
        ["day"], abs_tolerance=0.0, rel_tolerance=0.0,
        chunk_size=1,
    )
    assert result["natural_keys"] == ["day"]
    assert result["meta"]["partition_count"] == 2
    assert result["clusters"][0]["affected_keys"] == 1


def test_meta_records_strategy_and_rationale(detail_pair):
    rows = [("east", "x1", 1.0)]
    result = attribute_differences(
        detail_pair(rows, rows), ["region", "segment"],
        abs_tolerance=0.0, rel_tolerance=0.0,
    )
    assert result["key_strategy"] == "dsl_declared"
    assert "natural" in result["key_strategy_rationale"]
    assert result["fallback_policy"]
    assert result["meta"]["natural_key_source"] == "dsl"


def test_attribution_requires_two_implementations(tmp_path):
    only_one = write_detail(tmp_path / "a.jsonl", ["k", "v"], [("x", 1.0)])
    with pytest.raises(attribution.AttributionError):
        attribute_differences(
            {"impl_a": only_one}, ["k"], abs_tolerance=0.0, rel_tolerance=0.0
        )


def test_large_input_stays_bounded_with_single_row_chunks(detail_pair, tmp_path):
    # 行数远超 chunk_size：强制每个分区只有一行，验证流式分盘不会撑爆内存
    size = 2500
    # 固定 10% 相对偏离，全部落在同一分箱，保证归为一簇
    rows_a = [("east", f"k{i:05d}", 100.0) for i in range(size)]
    rows_b = [("east", f"k{i:05d}", 110.0) for i in range(size)]
    result = attribute_differences(
        detail_pair(rows_a, rows_b), ["region", "segment"],
        abs_tolerance=0.0, rel_tolerance=0.0,
        chunk_size=1, max_representatives=5, spill_root=str(tmp_path),
    )
    assert result["status"] == "ok"
    assert result["total_aligned_keys"] == size
    assert result["clusters"][0]["affected_keys"] == size
    assert result["meta"]["partition_count"] == size
    assert len(result["clusters"][0]["representative_keys"]) == 5
    # 全部暂存产物（run 目录与分区目录）都应被清理
    assert not list(tmp_path.glob("caliber-*"))
