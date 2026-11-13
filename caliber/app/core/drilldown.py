"""多维下钻与失败归因：在既有执行结果产物上做聚合、聚簇与降级兼容。

概念映射（指标一致性语境）：

- 规则：一个指标的一致性校验（``metric_key`` @ version）。
- 字段：被校验的度量字段，即指标本身；为将来列级血缘预留。
- 规则类型：``value_mismatch``（一致性）/ ``missing_dimension``（完整性）。
- 数据集：产生失败维度的实现来源（impl source）。
- 失败样例值：各实现在失败维度上的原始返回值（缺失为 ``None``）。
- 批次号：产生该失败的运行 ID（``run_id``）。

结果产物契约：执行结果 ``result_json`` 内含 ``failures`` 列表即视为新版
（schema 2）；缺少该键的老版本产物不报错，自动降级为按规则维度汇总并
给出明确提示。

内存契约：仓库路径按快照 keyset 分块流式读取，聚合状态只与聚合键数量
成正比，不会把全部失败明细一次性读进内存；样例值计数有上限，溢出计入
``__other__`` 桶。JSONL 结果文件逐行流式处理。

确定性契约：同一输入必然产生同一输出。所有计数、聚簇、代表值选取都只
依赖输入内容与显式排序，不依赖字典序、随机数或时钟（``generated_at``
除外，它不参与簇划分）。
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from pathlib import Path
from typing import Any, Iterator

from ..errors import CaliberError
from ..models import ComparisonResult, FailureRecord, MetricDefinition, utc_now

RESULT_SCHEMA_VERSION = 2
DRILLDOWN_SCHEMA = "caliber.drilldown/1.0"
DEFAULT_CHUNK_SIZE = 500
DEFAULT_MAX_REPRESENTATIVES = 5
DEFAULT_TOP_VALUES = 20
MAX_TRACKED_VALUES = 10_000
OTHER_BUCKET = "__other__"
MISSING_VALUE = "__missing__"

LEGACY_NOTICE = (
    "结果产物缺少失败样例字段（failures），属于旧版本执行结果；"
    "已降级为按规则维度汇总，多维下钻与聚簇不可用。"
    "重新执行比对后即可获得完整归因。"
)
PARTIAL_NOTICE = (
    "部分结果产物缺少失败样例字段（旧版本执行结果），"
    "这些快照仅计入按规则维度汇总，不参与多维下钻与聚簇。"
)
JSONL_NOTICE = "JSONL 输入不含校验总量信息，影响行数占比不可用（null）。"


class DrilldownError(CaliberError):
    code = "drilldown_error"


# ---------------------------------------------------------------------------
# 失败明细构建（执行器扩展：把 diff 提升为可下钻的失败记录）
# ---------------------------------------------------------------------------


def build_failures(
    comparison: ComparisonResult,
    metric: MetricDefinition,
    evaluations: dict[str, Any],
    *,
    batch: int | None,
) -> list[dict[str, Any]]:
    """Convert comparison diffs into failure records stored in the result.

    ``batch`` is the producing run id.  Records follow the deterministic
    dimension ordering of ``comparison.diffs`` so re-runs of the same input
    yield byte-identical payloads.
    """
    sources = {impl_id: item.source for impl_id, item in evaluations.items()}
    records: list[dict[str, Any]] = []
    for diff in comparison.diffs:
        missing = [impl_id for impl_id, value in diff.values.items() if value is None]
        if diff.kind == "missing_dimension" and missing:
            datasets = sorted({sources.get(impl_id, impl_id) for impl_id in missing})
        else:
            datasets = sorted({sources.get(impl_id, impl_id) for impl_id in diff.values})
        samples = [
            {
                "impl_id": impl_id,
                "source": sources.get(impl_id, impl_id),
                "value": diff.values.get(impl_id),
            }
            for impl_id in comparison.impl_ids
        ]
        records.append(
            FailureRecord(
                rule=comparison.metric_key,
                rule_version=comparison.metric_version,
                field=metric.key,
                rule_type=diff.kind,
                dimension=diff.dimension,
                datasets=datasets,
                samples=samples,
                max_delta=diff.max_delta,
                batch=batch,
            ).to_dict()
        )
    return records


# ---------------------------------------------------------------------------
# 流式来源：仓库分块读取 / 结果文件
# ---------------------------------------------------------------------------


def iter_snapshot_events(
    conn: sqlite3.Connection,
    *,
    run_id: int | None = None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> Iterator[dict[str, Any]]:
    """Yield one event per snapshot using keyset pagination.

    Memory stays bounded to ``chunk_size`` rows; the aggregator consumes and
    discards each event before the next chunk is fetched.
    """
    size = max(1, int(chunk_size))
    last_id = 0
    while True:
        if run_id is None:
            rows = conn.execute(
                "SELECT * FROM snapshots WHERE id > ? ORDER BY id LIMIT ?",
                (last_id, size),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM snapshots WHERE run_id = ? AND id > ? ORDER BY id LIMIT ?",
                (run_id, last_id, size),
            ).fetchall()
        if not rows:
            return
        for row in rows:
            last_id = int(row["id"])
            result = json.loads(row["result_json"])
            summary = json.loads(row["summary_json"])
            failures = result.get("failures")
            yield {
                "modern": failures is not None,
                "snapshot_id": int(row["id"]),
                "batch": int(row["run_id"]),
                "metric_key": row["metric_key"],
                "metric_version": row["metric_version"],
                "verdict": row["verdict"],
                "dimension_count": int(summary.get("dimension_count", 0)),
                "diff_count": int(summary.get("diff_count", 0)),
                "failures": list(failures) if failures is not None else [],
            }


def _event_from_report_metric(item: dict[str, Any], batch: int | None) -> dict[str, Any]:
    summary = item.get("summary") or {}
    failures = item.get("failures")
    return {
        "modern": failures is not None,
        "snapshot_id": item.get("snapshot_id"),
        "batch": batch,
        "metric_key": item.get("key", ""),
        "metric_version": int(item.get("version", 0)),
        "verdict": item.get("verdict", ""),
        "dimension_count": int(summary.get("dimension_count", 0)),
        "diff_count": int(summary.get("diff_count", 0)),
        "failures": list(failures) if failures is not None else [],
    }


def _event_from_result_doc(doc: dict[str, Any], batch: int | None) -> dict[str, Any]:
    summary = doc.get("summary") or {}
    failures = doc.get("failures")
    return {
        "modern": failures is not None,
        "snapshot_id": None,
        "batch": batch,
        "metric_key": doc.get("metric_key", ""),
        "metric_version": int(doc.get("metric_version", 0)),
        "verdict": doc.get("verdict", ""),
        "dimension_count": int(summary.get("dimension_count", 0)),
        "diff_count": int(summary.get("diff_count", 0)),
        "failures": list(failures) if failures is not None else [],
    }


def iter_file_events(path: str | Path) -> Iterator[dict[str, Any]]:
    """Stream failure events from an exported result file.

    Supported inputs:

    - 报告 JSON（``report --format json`` 产物，含 ``metrics`` 列表）；
    - 单个执行结果 JSON（快照 ``result_json`` 导出）；
    - JSONL：每行一个失败记录，逐行流式处理，适合超大结果集。
    """
    target = Path(path)
    if not target.is_file():
        raise DrilldownError(f"结果文件不存在: {target}")
    if target.suffix.lower() == ".jsonl":
        with target.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                text = line.strip()
                if not text:
                    continue
                try:
                    record = json.loads(text)
                except json.JSONDecodeError as exc:
                    raise DrilldownError(
                        f"JSONL 第 {line_number} 行不是合法 JSON: {exc}"
                    ) from exc
                if not isinstance(record, dict) or "rule" not in record:
                    raise DrilldownError(
                        f"JSONL 第 {line_number} 行不是失败记录（缺少 rule 字段）"
                    )
                yield {
                    "modern": True,
                    "snapshot_id": None,
                    "batch": record.get("batch"),
                    "metric_key": str(record.get("rule", "")),
                    "metric_version": int(record.get("rule_version", 0) or 0),
                    "verdict": "",
                    "dimension_count": 0,
                    "diff_count": 1,
                    "failures": [record],
                }
        return
    try:
        doc = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise DrilldownError(f"结果文件不是合法 JSON: {target}: {exc}") from exc
    if not isinstance(doc, dict):
        raise DrilldownError("结果文件顶层必须是 JSON 对象")
    if isinstance(doc.get("metrics"), list):
        batch = (doc.get("run") or {}).get("id")
        for item in doc["metrics"]:
            if isinstance(item, dict):
                yield _event_from_report_metric(item, batch)
        return
    if "metric_key" in doc or "failures" in doc:
        yield _event_from_result_doc(doc, None)
        return
    raise DrilldownError(
        "无法识别的结果文件格式：既不是导出的报告 JSON，也不是单个执行结果，"
        "也不是 JSONL 失败记录流"
    )


# ---------------------------------------------------------------------------
# 聚合器：增量、有界、确定性
# ---------------------------------------------------------------------------


def _delta_bucket(max_delta: Any) -> str:
    """Order-of-magnitude bucket for a mismatch delta; deterministic."""
    if max_delta is None:
        return "na"
    try:
        value = abs(float(max_delta))
    except (TypeError, ValueError):
        return "na"
    if value == 0:
        return "0"
    return f"1e{math.floor(math.log10(value))}"


def _value_key(value: Any) -> str:
    if value is None:
        return MISSING_VALUE
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _ratio(part: int, whole: int) -> float | None:
    if whole <= 0:
        return None
    return round(part / whole, 6)


def _attribution_hint(rule_type: str, fields: set[str], batches: set[int]) -> str:
    if len(fields) >= 2 and len(batches) == 1:
        return "疑似整批采集异常：多个字段在同一批次出现同特征失败"
    if len(fields) == 1 and len(batches) >= 2:
        return "疑似字段级脏数据：同一字段跨多个批次持续同特征失败"
    if rule_type == "missing_dimension":
        return "疑似数据缺失：维度在部分数据集中未到达"
    return "特征聚集度有限，建议结合代表性失败值人工核查"


class DrilldownAggregator:
    """Incremental aggregator with bounded memory and deterministic output."""

    def __init__(self, *, max_representatives: int = DEFAULT_MAX_REPRESENTATIVES,
                 top_values: int = DEFAULT_TOP_VALUES) -> None:
        self.max_representatives = max(1, int(max_representatives))
        self.top_values = max(1, int(top_values))
        self.snapshot_count = 0
        self.modern_count = 0
        self.legacy_count = 0
        self.total_checked = 0
        self.total_failures = 0
        self.known_denominator = True
        self._by_rule: dict[str, dict[str, Any]] = {}
        self._by_dataset: dict[str, dict[str, Any]] = {}
        self._by_field: dict[str, dict[str, Any]] = {}
        self._by_rule_type: dict[str, dict[str, Any]] = {}
        self._by_value: dict[str, dict[str, Any]] = {}
        self._value_overflow = 0
        self._clusters: dict[tuple[str, str, str], dict[str, Any]] = {}

    # -- 摄入 -------------------------------------------------------------

    def add_event(self, event: dict[str, Any]) -> None:
        self.snapshot_count += 1
        checked = int(event.get("dimension_count", 0))
        self.total_checked += checked
        rule_key = str(event.get("metric_key", ""))
        rule = self._by_rule.setdefault(
            rule_key,
            {
                "rule": rule_key,
                "versions": set(),
                "batches": set(),
                "runs": 0,
                "dimensions_checked": 0,
                "failures": 0,
                "legacy_diffs": 0,
                "verdicts": {},
            },
        )
        rule["runs"] += 1
        rule["dimensions_checked"] += checked
        version = event.get("metric_version")
        if version:
            rule["versions"].add(int(version))
        batch = event.get("batch")
        verdict = str(event.get("verdict", ""))
        if verdict:
            rule["verdicts"][verdict] = rule["verdicts"].get(verdict, 0) + 1
        if not event.get("modern"):
            self.legacy_count += 1
            legacy_diffs = int(event.get("diff_count", 0))
            rule["legacy_diffs"] += legacy_diffs
            if batch is not None and legacy_diffs > 0:
                rule["batches"].add(int(batch))
            return
        self.modern_count += 1
        for record in event.get("failures", []):
            self.add_failure(record, rule)

    def add_failure(self, record: dict[str, Any], rule: dict[str, Any]) -> None:
        self.total_failures += 1
        rule["failures"] += 1
        batch = record.get("batch")
        if batch is not None:
            rule["batches"].add(int(batch))
        rule_type = str(record.get("rule_type", "unknown"))
        field = str(record.get("field") or record.get("rule", ""))
        datasets = sorted({str(item) for item in record.get("datasets", [])})

        self._bump(self._by_dataset, datasets, record, batch)
        self._bump(self._by_field, [field], record, batch)
        self._bump(self._by_rule_type, [rule_type], record, batch)
        self._track_values(record)
        self._track_cluster(record, rule_type, datasets, field, batch)

    def _bump(self, table: dict[str, dict[str, Any]], keys: list[str],
              record: dict[str, Any], batch: Any) -> None:
        for key in keys or ["__unknown__"]:
            entry = table.setdefault(key, {"failures": 0, "rules": set(), "batches": set()})
            entry["failures"] += 1
            entry["rules"].add(str(record.get("rule", "")))
            if batch is not None:
                entry["batches"].add(int(batch))

    def _track_values(self, record: dict[str, Any]) -> None:
        for sample in record.get("samples", []):
            value = sample.get("value")
            key = _value_key(value)
            entry = self._by_value.get(key)
            if entry is None:
                if len(self._by_value) >= MAX_TRACKED_VALUES:
                    self._value_overflow += 1
                    key = OTHER_BUCKET
                    entry = self._by_value.setdefault(
                        key, {"value": OTHER_BUCKET, "count": 0, "rules": set()}
                    )
                else:
                    entry = self._by_value.setdefault(
                        key, {"value": value, "count": 0, "rules": set()}
                    )
            entry["count"] += 1
            entry["rules"].add(str(record.get("rule", "")))

    def _track_cluster(self, record: dict[str, Any], rule_type: str,
                       datasets: list[str], field: str, batch: Any) -> None:
        signature = (rule_type, "+".join(datasets), _delta_bucket(record.get("max_delta")))
        cluster = self._clusters.get(signature)
        if cluster is None:
            cluster = self._clusters[signature] = {
                "count": 0,
                "batches": set(),
                "rules": set(),
                "fields": set(),
                "representatives": [],
                "seen_values": set(),
            }
        cluster["count"] += 1
        if batch is not None:
            cluster["batches"].add(int(batch))
        cluster["rules"].add(str(record.get("rule", "")))
        cluster["fields"].add(field)
        if len(cluster["representatives"]) >= self.max_representatives:
            return
        values = {
            str(sample.get("impl_id")): sample.get("value")
            for sample in record.get("samples", [])
        }
        marker = json.dumps(values, ensure_ascii=False, sort_keys=True)
        if marker in cluster["seen_values"]:
            return
        cluster["seen_values"].add(marker)
        cluster["representatives"].append(
            {
                "rule": str(record.get("rule", "")),
                "dimension": str(record.get("dimension", "")),
                "batch": batch,
                "values": values,
            }
        )

    # -- 输出 -------------------------------------------------------------

    def _cluster_entries(self) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for (rule_type, dataset_key, bucket), cluster in self._clusters.items():
            datasets = dataset_key.split("+") if dataset_key else []
            signature_text = json.dumps(
                [rule_type, datasets, bucket], ensure_ascii=False, sort_keys=True
            )
            cluster_id = "clu_" + hashlib.sha1(signature_text.encode("utf-8")).hexdigest()[:10]
            batches = cluster["batches"]
            entries.append(
                {
                    "cluster_id": cluster_id,
                    "signature": {
                        "rule_type": rule_type,
                        "datasets": datasets,
                        "delta_bucket": bucket,
                    },
                    "failure_count": cluster["count"],
                    "affected_ratio": _ratio(cluster["count"], self.total_checked),
                    "first_batch": min(batches) if batches else None,
                    "last_batch": max(batches) if batches else None,
                    "batch_count": len(batches),
                    "rules": sorted(cluster["rules"]),
                    "fields": sorted(cluster["fields"]),
                    "hint": _attribution_hint(rule_type, cluster["fields"], batches),
                    "representatives": cluster["representatives"],
                }
            )
        entries.sort(key=lambda item: (-item["failure_count"], item["cluster_id"]))
        return entries

    def _dimension_entries(self, table: dict[str, dict[str, Any]], name: str) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for key, entry in table.items():
            batches = entry["batches"]
            entries.append(
                {
                    name: key,
                    "failures": entry["failures"],
                    "affected_ratio": _ratio(entry["failures"], self.total_checked),
                    "rules": sorted(entry["rules"]),
                    "first_batch": min(batches) if batches else None,
                    "last_batch": max(batches) if batches else None,
                }
            )
        entries.sort(key=lambda item: (-item["failures"], str(item[name])))
        return entries

    def _rule_entries(self) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for rule in self._by_rule.values():
            batches = rule["batches"]
            checked = rule["dimensions_checked"]
            total = rule["failures"] + rule["legacy_diffs"]
            entries.append(
                {
                    "rule": rule["rule"],
                    "versions": sorted(rule["versions"]),
                    "runs": rule["runs"],
                    "dimensions_checked": checked,
                    "failures": rule["failures"],
                    "legacy_diffs": rule["legacy_diffs"],
                    "affected_ratio": _ratio(total, checked),
                    "verdicts": dict(sorted(rule["verdicts"].items())),
                    "first_batch": min(batches) if batches else None,
                    "last_batch": max(batches) if batches else None,
                }
            )
        entries.sort(key=lambda item: (-(item["failures"] + item["legacy_diffs"]), item["rule"]))
        return entries

    def _value_entries(self) -> list[dict[str, Any]]:
        entries = [
            {"value": entry["value"], "count": entry["count"], "rules": sorted(entry["rules"])}
            for entry in self._by_value.values()
        ]
        entries.sort(key=lambda item: (-item["count"], _value_key(item["value"])))
        return entries[: self.top_values]

    def finish(self, *, scope: dict[str, Any]) -> dict[str, Any]:
        if self.legacy_count == 0:
            mode = "full"
        elif self.modern_count == 0:
            mode = "legacy"
        else:
            mode = "partial"
        notices: list[str] = []
        if mode == "legacy":
            notices.append(LEGACY_NOTICE)
        elif mode == "partial":
            notices.append(PARTIAL_NOTICE)
        if not self.known_denominator:
            notices.append(JSONL_NOTICE)
        if self._value_overflow:
            notices.append(
                f"失败样例值基数超过上限 {MAX_TRACKED_VALUES}，"
                f"溢出 {self._value_overflow} 次计入 {OTHER_BUCKET} 桶。"
            )
        return {
            "schema": DRILLDOWN_SCHEMA,
            "generated_at": utc_now(),
            "scope": scope,
            "mode": mode,
            "degraded": mode != "full",
            "notices": notices,
            "summary": {
                "snapshots": self.snapshot_count,
                "modern_results": self.modern_count,
                "legacy_results": self.legacy_count,
                "dimensions_checked": self.total_checked,
                "total_failures": self.total_failures,
                "legacy_diffs": sum(rule["legacy_diffs"] for rule in self._by_rule.values()),
                "cluster_count": len(self._clusters),
            },
            "by_rule": self._rule_entries(),
            "by_dataset": self._dimension_entries(self._by_dataset, "dataset"),
            "by_field": self._dimension_entries(self._by_field, "field"),
            "by_rule_type": self._dimension_entries(self._by_rule_type, "rule_type"),
            "by_sample_value": self._value_entries(),
            "clusters": self._cluster_entries(),
        }


# ---------------------------------------------------------------------------
# 顶层构建入口
# ---------------------------------------------------------------------------


def build_drilldown(
    conn: sqlite3.Connection,
    *,
    run_id: int | None = None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    max_representatives: int = DEFAULT_MAX_REPRESENTATIVES,
) -> dict[str, Any]:
    """Aggregate execution results from the repository (chunked, streaming)."""
    aggregator = DrilldownAggregator(max_representatives=max_representatives)
    for event in iter_snapshot_events(conn, run_id=run_id, chunk_size=chunk_size):
        aggregator.add_event(event)
    scope = {
        "source": "repository",
        "run_id": run_id if run_id is not None else "all",
        "chunk_size": max(1, int(chunk_size)),
    }
    return aggregator.finish(scope=scope)


def build_drilldown_from_file(
    path: str | Path,
    *,
    max_representatives: int = DEFAULT_MAX_REPRESENTATIVES,
) -> dict[str, Any]:
    """Aggregate an exported result file (report JSON / result JSON / JSONL)."""
    target = Path(path)
    aggregator = DrilldownAggregator(max_representatives=max_representatives)
    if target.suffix.lower() == ".jsonl":
        aggregator.known_denominator = False
    for event in iter_file_events(target):
        aggregator.add_event(event)
    if aggregator.snapshot_count == 0:
        raise DrilldownError(f"结果文件为空或不含任何执行结果: {target}")
    scope = {"source": "file", "file": str(target)}
    return aggregator.finish(scope=scope)


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------


def render_drilldown_json(report: dict[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)


def _fmt_ratio(value: float | None) -> str:
    return "-" if value is None else f"{value:.2%}"


def _fmt_value(value: Any) -> str:
    if value is None:
        return "缺失"
    return str(value).replace("|", "\\|").replace("\n", " ")


def render_drilldown_markdown(report: dict[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        "# 规则执行结果多维下钻与归因",
        "",
        f"- 模式：{report['mode']}" + ("（已降级）" if report["degraded"] else ""),
        f"- 结果快照：{summary['snapshots']}（新版 {summary['modern_results']} / "
        f"旧版 {summary['legacy_results']}）",
        f"- 校验维度总数：{summary['dimensions_checked']}",
        f"- 失败记录总数：{summary['total_failures']}"
        + (f"（另有旧版差异 {summary['legacy_diffs']} 条）" if summary["legacy_diffs"] else ""),
        f"- 问题簇：{summary['cluster_count']}",
        "",
    ]
    for notice in report["notices"]:
        lines.append(f"> ⚠ {notice}")
    if report["notices"]:
        lines.append("")
    lines.extend(["## 按规则汇总", "", "| 规则 | 运行次数 | 校验维度 | 失败 | 影响占比 | 批次 |", "| --- | --- | --- | --- | --- | --- |"])
    for item in report["by_rule"]:
        first_batch, last_batch = item["first_batch"], item["last_batch"]
        if first_batch is None:
            batch_text = "-"
        elif first_batch == last_batch:
            batch_text = str(first_batch)
        else:
            batch_text = f"{first_batch}~{last_batch}"
        lines.append(
            f"| {item['rule']} | {item['runs']} | {item['dimensions_checked']} | "
            f"{item['failures'] + item['legacy_diffs']} | "
            f"{_fmt_ratio(item['affected_ratio'])} | {batch_text} |"
        )
    lines.append("")
    for title, key in (("按数据集", "dataset"), ("按字段", "field"), ("按规则类型", "rule_type")):
        entries = report[f"by_{key}"]
        lines.extend([f"## {title}聚合", "", f"| {title[1:]} | 失败 | 影响占比 | 涉及规则 |", "| --- | --- | --- | --- |"])
        for entry in entries:
            lines.append(
                f"| {entry[key]} | {entry['failures']} | "
                f"{_fmt_ratio(entry['affected_ratio'])} | {', '.join(entry['rules'])} |"
            )
        lines.append("")
    lines.extend(["## 高频失败样例值", "", "| 样例值 | 次数 | 涉及规则 |", "| --- | --- | --- |"])
    for entry in report["by_sample_value"]:
        lines.append(
            f"| {_fmt_value(entry['value'])} | {entry['count']} | {', '.join(entry['rules'])} |"
        )
    lines.extend(["", "## 问题簇", ""])
    if not report["clusters"]:
        lines.append("无可聚簇的失败记录。")
    for cluster in report["clusters"]:
        signature = cluster["signature"]
        lines.extend(
            [
                f"### 簇 `{cluster['cluster_id']}`（{cluster['failure_count']} 条失败，"
                f"占比 {_fmt_ratio(cluster['affected_ratio'])}）",
                "",
                f"- 特征：类型 {signature['rule_type']} · 数据集 "
                f"{', '.join(signature['datasets']) or '-'} · 差异量级 {signature['delta_bucket']}",
                f"- 批次：首次 {cluster['first_batch']}，最近 {cluster['last_batch']}",
                f"- 涉及字段：{', '.join(cluster['fields'])}",
                f"- 归因提示：{cluster['hint']}",
                "- 代表性失败值：",
            ]
        )
        for rep in cluster["representatives"]:
            values = ", ".join(
                f"{impl}={_fmt_value(value)}" for impl, value in rep["values"].items()
            )
            lines.append(
                f"  - `{rep['rule']}` 维度 {rep['dimension']}（批次 {rep['batch']}）：{values}"
            )
        lines.append("")
    return "\n".join(lines)


def export_drilldown(report: dict[str, Any], fmt: str) -> tuple[str, str]:
    normalized = fmt.lower().lstrip(".")
    renderers = {"json": render_drilldown_json, "md": render_drilldown_markdown}
    if normalized not in renderers:
        raise DrilldownError(f"不支持的下钻报告格式: {fmt}")
    return normalized, renderers[normalized](report)
