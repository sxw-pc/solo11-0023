"""Streaming natural-key alignment and difference-cluster attribution.

The aligner never holds two implementations' full detail rows in memory at
once.  Each implementation's JSONL spill is split into deterministic
partitions by hashing the natural key; partitions are then aligned one at a
time, so resident memory is bounded by ``chunk_size`` rows per implementation
plus the bounded cluster accumulators.

Design choice (also recorded in every attribution block's meta):
natural keys are declared explicitly in the metric DSL (``natural_keys``)
rather than inferred from result column names.  A natural key is part of the
metric's semantic contract; name inference can silently misalign composite or
aliased keys, and a wrong alignment is worse than no alignment.  Metrics
without declared keys degrade to "skipped" and keep their dimension diffs.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable, TextIO

# 差异类型：仅一侧存在 / 数值偏离 / 类型不一致
DIFF_ONLY_ONE_SIDE = "only_one_side"
DIFF_VALUE_DEVIATION = "value_deviation"
DIFF_TYPE_MISMATCH = "type_mismatch"

KEY_STRATEGY = "dsl_declared"
STRATEGY_RATIONALE = (
    "自然键在指标 DSL 中显式声明（natural_keys，支持多列复合键），"
    "不从结果列名推断：自然键是指标的语义契约，列名推断在别名、复合键场景会静默错配，"
    "错误对齐比不对齐危害更大；显式声明可校验、可版本化，且与指标定义一起进入审计快照。"
)
FALLBACK_POLICY = (
    "未声明 natural_keys 时跳过明细归因（status=skipped），仅保留维度差值并在报告侧提示；"
    "声明的键列在任一实现结果中缺失、或归因过程发生异常时，该指标标记为 degraded，"
    "维度比对结果不受影响，整批 run 不中断。"
)

# 相对偏离的确定性分箱边界（左开右闭），保证两次运行簇划分稳定
REL_BANDS: tuple[tuple[float, str], ...] = (
    (1e-4, "(0, 0.01%]"),
    (1e-3, "(0.01%, 0.1%]"),
    (1e-2, "(0.1%, 1%]"),
    (1e-1, "(1%, 10%]"),
    (1.0, "(10%, 100%]"),
    (math.inf, "> 100%"),
)
TYPE_ORDER = {"null": 0, "integer": 1, "float": 2, "string": 3}
DIFF_TYPE_ORDER = {
    DIFF_ONLY_ONE_SIDE: 0,
    DIFF_TYPE_MISMATCH: 1,
    DIFF_VALUE_DEVIATION: 2,
}


class AttributionError(Exception):
    """Raised when attribution cannot proceed; callers degrade the metric."""


def value_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "integer"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "float"
    return "string"


def is_numeric(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _key_sort_key(key: tuple[Any, ...]) -> str:
    return json.dumps(list(key), ensure_ascii=False, default=str, sort_keys=True)


def _hash_partition(key: tuple[Any, ...], partition_count: int) -> int:
    digest = hashlib.blake2b(
        _key_sort_key(key).encode("utf-8"), digest_size=8
    ).digest()
    return int.from_bytes(digest, "big") % partition_count


def _read_header(path: str | Path) -> list[str]:
    with open(path, "r", encoding="utf-8") as handle:
        first = handle.readline()
    if not first:
        raise AttributionError(f"明细暂存文件为空: {path}")
    columns = json.loads(first)
    if not isinstance(columns, list) or not columns:
        raise AttributionError(f"明细暂存文件缺少列头: {path}")
    return [str(column) for column in columns]


def _count_rows(path: str | Path) -> int:
    with open(path, "rb") as handle:
        return sum(1 for _ in handle) - 1  # 首行为列头


def _iter_rows(path: str | Path) -> Iterable[dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as handle:
        handle.readline()
        for line in handle:
            line = line.strip()
            if line:
                yield json.loads(line)


def _deviation_band(max_delta: float, scale: float) -> str:
    if scale <= 0.0:
        order = math.floor(math.log10(max_delta)) if max_delta > 0 else 0
        return f"绝对偏离 ~1e{order}"
    relative = max_delta / scale
    for upper, label in REL_BANDS:
        if relative <= upper:
            return label
    return "> 100%"


class _Cluster:
    """单个差异簇；代表键有界，内存占用不随全量明细增长。"""

    __slots__ = (
        "diff_type",
        "signature",
        "label",
        "affected_keys",
        "representatives",
    )

    def __init__(self, diff_type: str, signature: str, label: str) -> None:
        self.diff_type = diff_type
        self.signature = signature
        self.label = label
        self.affected_keys = 0
        self.representatives: list[dict[str, Any]] = []

    def add(
        self,
        key: tuple[Any, ...],
        values: dict[str, Any],
        max_representatives: int,
    ) -> None:
        self.affected_keys += 1
        entry = {"natural_key": list(key), "values": values}
        if len(self.representatives) < max_representatives:
            self.representatives.append(entry)
            self.representatives.sort(
                key=lambda item: _key_sort_key(tuple(item["natural_key"]))
            )
            return
        # 有界保留字典序最小的 N 个代表键，排序键确定 -> 多次运行结果稳定
        worst = self.representatives[-1]
        if _key_sort_key(key) < _key_sort_key(
            tuple(worst["natural_key"])
        ):
            self.representatives[-1] = entry
            self.representatives.sort(
                key=lambda item: _key_sort_key(tuple(item["natural_key"]))
            )

    def to_dict(self, total_keys: int) -> dict[str, Any]:
        return {
            "diff_type": self.diff_type,
            "signature": self.signature,
            "signature_label": self.label,
            "affected_keys": self.affected_keys,
            "affected_ratio": round(self.affected_keys / total_keys, 6)
            if total_keys
            else None,
            "representative_keys": [
                list(item["natural_key"]) for item in self.representatives
            ],
            "representative_rows": self.representatives,
        }


class _Partitioner:
    """把一份 JSONL 明细按自然键哈希切成多个分区文件（流式写盘）。"""

    def __init__(
        self, directory: str, impl_id: str, partition_count: int
    ) -> None:
        self.files: list[TextIO] = [
            open(
                os.path.join(directory, f"{impl_id}.part{index}.jsonl"),
                "w",
                encoding="utf-8",
            )
            for index in range(partition_count)
        ]

    def write(self, partition: int, row: dict[str, Any]) -> None:
        self.files[partition].write(
            json.dumps(row, ensure_ascii=False, default=str) + "\n"
        )

    def close(self) -> None:
        for handle in self.files:
            handle.close()


def _build_partitions(
    detail_files: dict[str, str],
    natural_keys: list[str],
    work_dir: str,
    chunk_size: int,
) -> tuple[dict[str, list[str]], int, dict[str, int], dict[str, int]]:
    row_counts = {impl_id: _count_rows(path) for impl_id, path in detail_files.items()}
    largest = max(row_counts.values(), default=0)
    partition_count = max(1, math.ceil(largest / max(1, chunk_size)))
    partition_paths: dict[str, list[str]] = {}
    duplicate_counts: dict[str, int] = {}
    for impl_id, source in detail_files.items():
        partitioner = _Partitioner(work_dir, impl_id, partition_count)
        seen: set[str] = set()
        duplicates = 0
        try:
            for row in _iter_rows(source):
                key = tuple(row.get(column) for column in natural_keys)
                marker = _key_sort_key(key)
                if marker in seen:
                    # 同一实现出现重复自然键：保留首行（文件顺序确定），只计数
                    duplicates += 1
                else:
                    seen.add(marker)
                    partitioner.write(
                        _hash_partition(key, partition_count), row
                    )
        finally:
            partitioner.close()
        partition_paths[impl_id] = [
            os.path.join(work_dir, f"{impl_id}.part{index}.jsonl")
            for index in range(partition_count)
        ]
        duplicate_counts[impl_id] = duplicates
    return partition_paths, partition_count, row_counts, duplicate_counts


def _iter_partition(path: str) -> Iterable[dict[str, Any]]:
    """Partition files contain only JSON rows (no header line)."""
    try:
        handle = open(path, "r", encoding="utf-8")
    except FileNotFoundError:
        return
    with handle:
        for line in handle:
            line = line.strip()
            if line:
                yield json.loads(line)


def _load_partition(
    path: str, natural_keys: list[str]
) -> dict[str, tuple[tuple[Any, ...], dict[str, Any]]]:
    aligned: dict[str, tuple[tuple[Any, ...], dict[str, Any]]] = {}
    for row in _iter_partition(path):
        key = tuple(row.get(column) for column in natural_keys)
        aligned.setdefault(_key_sort_key(key), (key, row))
    return aligned


def attribute_differences(
    detail_files: dict[str, str],
    natural_keys: list[str],
    *,
    abs_tolerance: float,
    rel_tolerance: float,
    chunk_size: int = 2048,
    max_representatives: int = 5,
    spill_root: str | None = None,
) -> dict[str, Any]:
    """按自然键对齐多实现明细并把差异聚成有界簇。

    ``detail_files`` 为 ``impl_id -> JSONL 暂存路径``，每个文件首行是列名
    数组、其后每行一条 JSON 明细。返回可直接写入快照的归因字典。
    """
    if not natural_keys:
        raise AttributionError("natural_keys 为空时不应执行归因")
    impl_ids = list(detail_files)
    if len(impl_ids) < 2:
        raise AttributionError("至少需要两个实现的明细才能对齐")

    headers = {impl_id: _read_header(path) for impl_id, path in detail_files.items()}
    missing_columns: dict[str, list[str]] = {}
    for impl_id, columns in headers.items():
        absent = [column for column in natural_keys if column not in columns]
        if absent:
            missing_columns[impl_id] = absent
    if missing_columns:
        return _skipped_or_degraded(
            "degraded",
            "natural_key_column_missing",
            natural_keys,
            impl_ids,
            detail={
                "missing_columns": missing_columns,
                "headers": headers,
            },
        )

    work_dir = tempfile.mkdtemp(prefix="caliber-attr-", dir=spill_root)
    try:
        partition_paths, partition_count, row_counts, duplicate_counts = (
            _build_partitions(detail_files, natural_keys, work_dir, chunk_size)
        )
        clusters: dict[tuple[str, str], _Cluster] = {}
        total_keys = 0
        affected = 0

        def cluster_for(diff_type: str, signature: str, label: str) -> _Cluster:
            marker = (diff_type, signature)
            cluster = clusters.get(marker)
            if cluster is None:
                cluster = _Cluster(diff_type, signature, label)
                clusters[marker] = cluster
            return cluster

        for partition in range(partition_count):
            loaded = {
                impl_id: _load_partition(
                    partition_paths[impl_id][partition], natural_keys
                )
                for impl_id in impl_ids
            }
            all_markers = sorted(
                {marker for mapping in loaded.values() for marker in mapping}
            )
            for marker in all_markers:
                total_keys += 1
                present = {
                    impl_id: loaded[impl_id][marker]
                    for impl_id in impl_ids
                    if marker in loaded[impl_id]
                }
                natural_key = next(iter(present.values()))[0]
                values = {
                    impl_id: row.get(headers[impl_id][-1])
                    for impl_id, (_, row) in present.items()
                }
                # 缺失侧补 None，保证代表值结构稳定、报告可直接渲染
                for impl_id in impl_ids:
                    values.setdefault(impl_id, None)
                if len(present) < len(impl_ids):
                    missing = [
                        impl_id for impl_id in impl_ids if impl_id not in present
                    ]
                    present_label = ",".join(present)
                    signature = f"present:{present_label}"
                    label = (
                        f"仅 {present_label} 存在；缺失实现: {', '.join(missing)}"
                    )
                    cluster = cluster_for(
                        DIFF_ONLY_ONE_SIDE, signature, label
                    )
                    cluster.add(natural_key, values, max_representatives)
                    affected += 1
                    continue

                types = {impl_id: value_type(value) for impl_id, value in values.items()}
                distinct_types = set(types.values())
                if len(distinct_types) > 1 and distinct_types & {"null", "string"}:
                    # 字符串/NULL 与数值并存 -> 类型不一致；纯数值内 int/float
                    # 差异只是 SQLite 存储类型，按数值偏离处理
                    signature = "types:" + "|".join(
                        f"{impl_id}:{types[impl_id]}" for impl_id in impl_ids
                    )
                    label = "类型组合 " + " / ".join(
                        f"{impl_id}={types[impl_id]}" for impl_id in impl_ids
                    )
                    cluster = cluster_for(DIFF_TYPE_MISMATCH, signature, label)
                    cluster.add(natural_key, values, max_representatives)
                    affected += 1
                    continue

                numeric_values: dict[str, float] = {}
                try:
                    numeric_values = {
                        impl_id: float(value) for impl_id, value in values.items()
                    }
                except (TypeError, ValueError):
                    signature = "types:" + "|".join(
                        f"{impl_id}:{types[impl_id]}" for impl_id in impl_ids
                    )
                    label = "类型组合 " + " / ".join(
                        f"{impl_id}={types[impl_id]}" for impl_id in impl_ids
                    )
                    cluster = cluster_for(DIFF_TYPE_MISMATCH, signature, label)
                    cluster.add(natural_key, values, max_representatives)
                    affected += 1
                    continue
                max_delta = 0.0
                mismatch = False
                impl_order = list(numeric_values)
                for left in range(len(impl_order)):
                    for right in range(left + 1, len(impl_order)):
                        left_id, right_id = impl_order[left], impl_order[right]
                        delta = abs(
                            numeric_values[left_id] - numeric_values[right_id]
                        )
                        max_delta = max(max_delta, delta)
                        scale = max(
                            abs(numeric_values[left_id]),
                            abs(numeric_values[right_id]),
                        )
                        if delta > abs_tolerance + rel_tolerance * scale:
                            mismatch = True
                if mismatch:
                    scale = max(abs(value) for value in numeric_values.values())
                    band = _deviation_band(max_delta, scale)
                    signature = f"band:{band}"
                    cluster = cluster_for(
                        DIFF_VALUE_DEVIATION,
                        signature,
                        f"数值偏离分箱 {band}（最大绝对差 {max_delta:.6g}）",
                    )
                    cluster.add(natural_key, values, max_representatives)
                    affected += 1

        ordered = sorted(
            clusters.values(),
            key=lambda item: (
                DIFF_TYPE_ORDER.get(item.diff_type, 99),
                -item.affected_keys,
                item.signature,
            ),
        )
        return {
            "status": "ok",
            "natural_keys": list(natural_keys),
            "key_strategy": KEY_STRATEGY,
            "key_strategy_rationale": STRATEGY_RATIONALE,
            "fallback_policy": FALLBACK_POLICY,
            "degraded_reasons": [],
            "total_aligned_keys": total_keys,
            "affected_keys": affected,
            "affected_ratio": round(affected / total_keys, 6) if total_keys else 0.0,
            "clusters": [item.to_dict(total_keys) for item in ordered],
            "meta": {
                "natural_key_source": "dsl",
                "impl_ids": impl_ids,
                "row_counts": row_counts,
                "duplicate_natural_keys": duplicate_counts,
                "partition_count": partition_count,
                "partition_chunk_size": int(chunk_size),
                "max_representatives_per_cluster": int(max_representatives),
                "value_columns": {
                    impl_id: headers[impl_id][-1] for impl_id in impl_ids
                },
                "spilled_to_disk": True,
            },
        }
    finally:
        _safe_rmtree(work_dir)


def _safe_rmtree(directory: str) -> None:
    import shutil

    shutil.rmtree(directory, ignore_errors=True)


def skipped_attribution(natural_keys: list[str] | None = None) -> dict[str, Any]:
    """未声明自然键时的降级归因块：只保留提示，不执行任何明细读取。"""
    return {
        "status": "skipped",
        "natural_keys": list(natural_keys or []),
        "key_strategy": KEY_STRATEGY,
        "key_strategy_rationale": STRATEGY_RATIONALE,
        "fallback_policy": FALLBACK_POLICY,
        "degraded_reasons": [
            {
                "code": "natural_key_not_declared",
                "message": "指标 DSL 未声明 natural_keys，跳过明细归因，仅保留维度差值。",
            }
        ],
        "total_aligned_keys": 0,
        "affected_keys": 0,
        "affected_ratio": None,
        "clusters": [],
        "meta": {"natural_key_source": None, "spilled_to_disk": False},
    }


def _skipped_or_degraded(
    status: str,
    code: str,
    natural_keys: list[str],
    impl_ids: list[str],
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    reason = {"code": code}
    if code == "natural_key_column_missing":
        reason["message"] = "声明的自然键列在至少一个实现结果中缺失，无法对齐，已降级为仅维度比对。"
    else:
        reason["message"] = "明细归因执行失败，已降级为仅维度比对。"
    if detail:
        reason.update(detail)
    return {
        "status": status,
        "natural_keys": list(natural_keys),
        "key_strategy": KEY_STRATEGY,
        "key_strategy_rationale": STRATEGY_RATIONALE,
        "fallback_policy": FALLBACK_POLICY,
        "degraded_reasons": [reason],
        "total_aligned_keys": 0,
        "affected_keys": 0,
        "affected_ratio": None,
        "clusters": [],
        "meta": {
            "natural_key_source": "dsl",
            "impl_ids": impl_ids,
            "spilled_to_disk": False,
        },
    }


def degraded_from_error(
    natural_keys: list[str], impl_ids: list[str], exc: BaseException
) -> dict[str, Any]:
    return _skipped_or_degraded(
        "degraded",
        "attribution_error",
        natural_keys,
        impl_ids,
        detail={"error_type": type(exc).__name__, "error_message": str(exc)},
    )
