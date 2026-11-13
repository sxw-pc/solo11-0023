"""Artifact loading, field validation and cross-artifact normalization.

The loader never drops a record without emitting an :class:`Issue`.  Fatal
problems (missing file, malformed JSON, missing required fields, conflicting
duplicates) are reported with ``severity="error"``.  Recoverable problems
(dataset/rule name variants, unknown extra fields, identical duplicates) are
reported with ``severity="warning"`` and are merged/deduplicated explicitly.

Artifact contract (JSON)::

    {
      "artifact_version": "1.0",
      "rules": [
        {
          "rule_key": "amount_positive",
          "name": "金额为正",
          "datasets": [
            {"dataset": "orders_a", "hits": 120, "passes": 1180,
             "total": 1200}
          ]
        }
      ]
    }

A flat ``records`` list (or a bare top-level list) with the same fields is
also accepted, as is JSONL with one record per line.  Common field aliases
are listed in :data:`FIELD_ALIASES`.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable, Mapping

from .models import ArtifactMeta, Issue, LoadedArtifacts, RuleObservation

ARTIFACT_VERSION = "1.0"
SUPPORTED_ARTIFACT_VERSIONS = {"1.0"}
SUPPORTED_SUFFIXES = {".json", ".jsonl", ".ndjson"}

FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "rule_key": ("rule_key", "rule", "key"),
    "name": ("name", "rule_name", "title"),
    "dataset": ("dataset", "dataset_key", "source"),
    "hits": ("hits", "hit_count", "matched", "match_count"),
    "passes": ("passes", "pass_count", "passed", "pass"),
    "total": ("total", "sample_total", "total_samples", "samples", "row_count"),
}
KNOWN_RECORD_FIELDS = {alias for aliases in FIELD_ALIASES.values() for alias in aliases}
ALIAS_HINTS = " / ".join(FIELD_ALIASES["hits"])

_NORMALIZE_RE = re.compile(r"[^0-9a-z_一-鿿]+")
_REPEAT_UNDERSCORE_RE = re.compile(r"_+")


def normalize_name(raw: str) -> str:
    """Canonicalize a dataset or rule name for cross-artifact comparison.

    Lower-cases ASCII, turns any run of punctuation/whitespace into a single
    underscore and strips leading/trailing underscores.  Chinese characters
    are preserved.
    """
    lowered = raw.strip().lower()
    replaced = _NORMALIZE_RE.sub("_", lowered)
    return _REPEAT_UNDERSCORE_RE.sub("_", replaced).strip("_")


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _field(record: Mapping[str, Any], logical: str) -> tuple[Any, str | None]:
    """Return the first present alias value and the alias used.

    Raises :class:`ValueError` when two aliases carry conflicting values.
    """
    present: list[tuple[str, Any]] = [
        (alias, record[alias]) for alias in FIELD_ALIASES[logical] if alias in record
    ]
    if not present:
        return None, None
    first_alias, first_value = present[0]
    for alias, value in present[1:]:
        if value != first_value:
            raise ValueError(
                f"字段别名冲突：{first_alias}={first_value!r} 与 {alias}={value!r}"
            )
    return first_value, first_alias


def _to_int(value: Any, *, field: str, where: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{where} 的 {field} 不能是布尔值")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value.is_integer():
            return int(value)
        raise ValueError(f"{where} 的 {field} 必须是整数，实际为 {value!r}")
    if isinstance(value, str):
        text = value.strip()
        if re.fullmatch(r"-?\d+", text):
            return int(text)
        raise ValueError(f"{where} 的 {field} 必须是整数，实际为 {value!r}")
    if value is None:
        raise ValueError(f"{where} 缺少必填字段 {field}")
    raise ValueError(f"{where} 的 {field} 必须是整数，实际类型为 {type(value).__name__}")


def _describe(record: Mapping[str, Any], index: int, artifact: str) -> str:
    hint = ""
    for alias in FIELD_ALIASES["rule_key"]:
        if record.get(alias) not in (None, ""):
            hint = f"（规则 {record.get(alias)!r}）"
            break
    return f"{artifact} 第 {index + 1} 条记录{hint}"


def _safe_field(
    record: Mapping[str, Any],
    logical: str,
    where: str,
    issues: list[Issue],
    artifact: str,
) -> tuple[Any, str | None]:
    """Like :func:`_field` but records an issue instead of raising."""
    try:
        return _field(record, logical)
    except ValueError as exc:
        issues.append(
            Issue("field_alias_conflict", f"{where}: {exc}", "error", artifact=artifact)
        )
        return None, None


def _has_alias_conflict(record: Mapping[str, Any], logical: str) -> bool:
    try:
        _field(record, logical)
    except ValueError:
        return True
    return False


def _extract_records(
    payload: Any, artifact: str
) -> tuple[list[tuple[Mapping[str, Any], str | None]], list[Issue]]:
    """Flatten the supported artifact shapes into ``(record, rule_name)`` pairs."""
    issues: list[Issue] = []
    pairs: list[tuple[Mapping[str, Any], str | None]] = []

    def add_rule_container(item: Any, index: int) -> None:
        where = f"{artifact} 第 {index + 1} 个规则"
        if not isinstance(item, Mapping):
            issues.append(
                Issue("invalid_rule_entry", f"{where} 不是对象", "error", artifact=artifact)
            )
            return
        dataset_block = item.get("datasets", item.get("records"))
        if dataset_block is None:
            issues.append(
                Issue(
                    "missing_fields",
                    f"{where} 缺少 datasets 列表",
                    "error",
                    artifact=artifact,
                    detail={"required": sorted(FIELD_ALIASES)},
                )
            )
            return
        name, _ = _safe_field(item, "name", where, issues, artifact)
        rule_key, _ = _safe_field(item, "rule_key", where, issues, artifact)
        if rule_key is None and _has_alias_conflict(item, "rule_key"):
            # The conflict is already reported; skip the container so the
            # nested records do not also emit misleading "missing field" errors.
            return
        entries: Iterable[Any]
        if isinstance(dataset_block, Mapping):
            nested: list[Any] = []
            for dataset_name, counts in dataset_block.items():
                if not isinstance(counts, Mapping):
                    issues.append(
                        Issue(
                            "invalid_dataset_entry",
                            f"{where} 的数据集 {dataset_name!r} 不是对象",
                            "error",
                            artifact=artifact,
                        )
                    )
                    continue
                copied = dict(counts)
                copied.setdefault("dataset", dataset_name)
                nested.append(copied)
            entries = nested
        elif isinstance(dataset_block, list):
            entries = dataset_block
        else:
            issues.append(
                Issue(
                    "invalid_dataset_list",
                    f"{where} 的 datasets 必须是列表或对象",
                    "error",
                    artifact=artifact,
                )
            )
            return
        for entry in entries:
            if not isinstance(entry, Mapping):
                issues.append(
                    Issue(
                        "invalid_dataset_entry",
                        f"{where} 包含非对象的数据集条目",
                        "error",
                        artifact=artifact,
                        rule=str(rule_key) if rule_key is not None else None,
                    )
                )
                continue
            merged = dict(entry)
            if rule_key is not None:
                merged.setdefault("rule_key", rule_key)
            if name is not None:
                merged.setdefault("name", name)
            pairs.append((merged, str(name) if name is not None else None))

    if isinstance(payload, list):
        for item in payload:
            if isinstance(item, Mapping) and ("datasets" in item or "records" in item):
                add_rule_container(item, len(pairs))
            elif isinstance(item, Mapping):
                pairs.append((item, None))
            else:
                issues.append(
                    Issue(
                        "invalid_record",
                        f"{artifact} 包含非对象的记录",
                        "error",
                        artifact=artifact,
                    )
                )
    elif isinstance(payload, Mapping):
        version = payload.get("artifact_version")
        if version is not None and str(version) not in SUPPORTED_ARTIFACT_VERSIONS:
            issues.append(
                Issue(
                    "unsupported_artifact_version",
                    f"{artifact} 的 artifact_version={version!r} 不受支持，"
                    f"支持：{', '.join(sorted(SUPPORTED_ARTIFACT_VERSIONS))}",
                    "error",
                    artifact=artifact,
                )
            )
        if "rules" in payload:
            rules = payload["rules"]
            if not isinstance(rules, list):
                issues.append(
                    Issue(
                        "invalid_rules",
                        f"{artifact} 的 rules 必须是列表",
                        "error",
                        artifact=artifact,
                    )
                )
                rules = []
            for index, item in enumerate(rules):
                add_rule_container(item, index)
        elif "records" in payload:
            records = payload["records"]
            if not isinstance(records, list):
                issues.append(
                    Issue(
                        "invalid_records",
                        f"{artifact} 的 records 必须是列表",
                        "error",
                        artifact=artifact,
                    )
                )
                records = []
            for item in records:
                if isinstance(item, Mapping):
                    pairs.append((item, None))
                else:
                    issues.append(
                        Issue(
                            "invalid_record",
                            f"{artifact} 的 records 包含非对象条目",
                            "error",
                            artifact=artifact,
                        )
                    )
        elif any(alias in payload for aliases in FIELD_ALIASES.values() for alias in aliases):
            pairs.append((payload, None))
        else:
            issues.append(
                Issue(
                    "unrecognized_artifact",
                    f"{artifact} 无法识别：需要 rules 列表、records 列表或单条记录",
                    "error",
                    artifact=artifact,
                )
            )
    else:
        issues.append(
            Issue(
                "invalid_artifact_root",
                f"{artifact} 顶层必须是对象或列表",
                "error",
                artifact=artifact,
            )
        )
    return pairs, issues


def _read_payload(path: Path) -> tuple[Any, bytes]:
    raw = path.read_bytes()
    text = raw.decode("utf-8-sig")
    if path.suffix.lower() in {".jsonl", ".ndjson"}:
        items: list[Any] = []
        for line_number, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                items.append(json.loads(stripped))
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"{path} 第 {line_number} 行不是合法 JSON：{exc.msg}"
                ) from exc
        return items, raw
    return json.loads(text), raw


def _validate_records(
    pairs: Iterable[tuple[Mapping[str, Any], str | None]], artifact: str
) -> tuple[list[RuleObservation], list[Issue]]:
    observations: list[RuleObservation] = []
    issues: list[Issue] = []
    for index, (record, container_name) in enumerate(pairs):
        where = _describe(record, index, artifact)
        try:
            rule_raw, _ = _field(record, "rule_key")
            dataset_raw, _ = _field(record, "dataset")
        except ValueError as exc:
            issues.append(
                Issue("field_alias_conflict", f"{where}: {exc}", "error", artifact=artifact)
            )
            continue
        missing = []
        if rule_raw in (None, ""):
            missing.append("rule_key（别名 " + " / ".join(FIELD_ALIASES["rule_key"]) + "）")
        if dataset_raw in (None, ""):
            missing.append("dataset（别名 " + " / ".join(FIELD_ALIASES["dataset"]) + "）")
        for logical in ("hits", "passes", "total"):
            value, _ = _field(record, logical)
            if value is None:
                missing.append(logical + "（别名 " + " / ".join(FIELD_ALIASES[logical]) + "）")
        if missing:
            markdown_style = "、".join(missing)
            issues.append(
                Issue(
                    "missing_fields",
                    f"{where} 缺少必填字段：{markdown_style}；一条记录必须包含 "
                    "规则、数据集、命中数、通过数、样本总数",
                    "error",
                    artifact=artifact,
                )
            )
            continue

        rule_key = str(rule_raw).strip()
        dataset = str(dataset_raw).strip()
        if not normalize_name(rule_key):
            issues.append(
                Issue(
                    "empty_rule_key",
                    f"{where} 的规则名为空或只包含无法识别的字符",
                    "error",
                    artifact=artifact,
                )
            )
            continue
        if not normalize_name(dataset):
            issues.append(
                Issue(
                    "empty_dataset_name",
                    f"{where} 的数据集名 {dataset!r} 为空或只包含无法识别的字符",
                    "error",
                    artifact=artifact,
                    rule=rule_key,
                )
            )
            continue

        try:
            hits = _to_int(_field(record, "hits")[0], field="hits", where=where)
            passes = _to_int(_field(record, "passes")[0], field="passes", where=where)
            total = _to_int(_field(record, "total")[0], field="total", where=where)
        except ValueError as exc:
            issues.append(
                Issue("invalid_count", str(exc), "error", artifact=artifact,
                      dataset=dataset, rule=rule_key)
            )
            continue

        invalid = None
        if total <= 0:
            invalid = f"total 必须大于 0，实际为 {total}"
        elif hits < 0 or passes < 0:
            invalid = "hits 和 passes 不能为负数"
        elif hits > total:
            invalid = f"hits={hits} 不能大于 total={total}"
        elif passes > total:
            invalid = f"passes={passes} 不能大于 total={total}"
        if invalid:
            issues.append(
                Issue(
                    "invalid_count",
                    f"{where}: {invalid}",
                    "error",
                    artifact=artifact,
                    dataset=dataset,
                    rule=rule_key,
                )
            )
            continue

        name_value = container_name
        if name_value is None:
            raw_name, _ = _field(record, "name")
            name_value = str(raw_name) if raw_name not in (None, "") else None

        for key in record:
            if key not in KNOWN_RECORD_FIELDS:
                issues.append(
                    Issue(
                        "unknown_field",
                        f"{where} 包含未知字段 {key!r}，已忽略；命中/通过/总数请使用 "
                        f"{ALIAS_HINTS} 等字段",
                        "warning",
                        artifact=artifact,
                        dataset=dataset,
                        rule=rule_key,
                    )
                )
        observations.append(
            RuleObservation(
                artifact=artifact,
                dataset=dataset,
                rule_key=rule_key,
                name=name_value,
                hits=hits,
                passes=passes,
                total=total,
            )
        )
    return observations, issues


def load_artifacts(paths: Iterable[str | Path]) -> LoadedArtifacts:
    """Load and normalize one or more artifacts.

    Raises :class:`PowerInputError` only for unusable invocations (e.g. no
    paths).  Per-file and per-record problems are returned as issues so the
    caller can print them and decide whether to proceed.
    """
    from .errors import PowerInputError

    provided = [Path(item) for item in paths]
    if not provided:
        raise PowerInputError("至少需要一份规则执行产物")

    issues: list[Issue] = []
    observations: list[RuleObservation] = []
    metas: list[ArtifactMeta] = []
    seen_paths: set[str] = set()

    ordered = sorted(provided, key=lambda item: str(item.expanduser().resolve()))
    for raw_path in ordered:
        path = raw_path.expanduser().resolve()
        label = str(path)
        if label in seen_paths:
            issues.append(
                Issue(
                    "duplicate_artifact",
                    f"产物重复出现，已忽略后续副本：{label}",
                    "warning",
                    artifact=label,
                )
            )
            continue
        seen_paths.add(label)
        if not path.exists():
            issues.append(
                Issue("artifact_not_found", f"产物不存在：{label}", "error", artifact=label)
            )
            continue
        if not path.is_file():
            issues.append(
                Issue("artifact_not_file", f"产物不是文件：{label}", "error", artifact=label)
            )
            continue
        if path.suffix.lower() not in SUPPORTED_SUFFIXES:
            issues.append(
                Issue(
                    "unsupported_format",
                    f"不支持的产物格式 {path.suffix!r}（{label}），"
                    f"支持：{', '.join(sorted(SUPPORTED_SUFFIXES))}",
                    "error",
                    artifact=label,
                )
            )
            continue
        try:
            payload, raw = _read_payload(path)
        except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
            issues.append(
                Issue("artifact_parse_error", f"无法解析产物 {label}：{exc}",
                      "error", artifact=label)
            )
            continue

        pairs, extraction_issues = _extract_records(payload, label)
        issues.extend(extraction_issues)
        file_observations, record_issues = _validate_records(pairs, label)
        issues.extend(record_issues)
        if not file_observations and not record_issues and not extraction_issues:
            issues.append(
                Issue(
                    "artifact_no_records",
                    f"产物 {label} 已解析成功，但不包含任何规则记录",
                    "warning",
                    artifact=label,
                )
            )
        observations.extend(file_observations)
        metas.append(
            ArtifactMeta(
                path=label,
                digest=_digest(raw),
                dataset_count=len({normalize_name(item.dataset) for item in file_observations}),
                rule_count=len({normalize_name(item.rule_key) for item in file_observations}),
            )
        )

    (normalized, normalization_issues,
     dataset_aliases, rule_aliases) = _normalize_and_deduplicate(observations)
    issues.extend(normalization_issues)

    loaded = LoadedArtifacts(
        observations=normalized,
        artifacts=metas,
        dataset_aliases=dataset_aliases,
        rule_aliases=rule_aliases,
    )
    loaded.issues = sorted(issues, key=lambda item: item.sort_key)
    return loaded


def _collect_aliases(values: Iterable[str]) -> dict[str, list[str]]:
    """Map canonical names to all raw variants that collapse onto them."""
    groups: dict[str, set[str]] = {}
    for value in values:
        groups.setdefault(normalize_name(value), set()).add(value)
    return {
        canonical: sorted(variants)
        for canonical, variants in sorted(groups.items())
        if len(variants) > 1
    }


def _record_alias_groups(values: Iterable[str], kind: str) -> tuple[dict[str, str], list[Issue]]:
    groups: dict[str, set[str]] = {}
    for value in values:
        groups.setdefault(normalize_name(value), set()).add(value)
    issues: list[Issue] = []
    mapping: dict[str, str] = {}
    labels = {"dataset": "数据集", "rule": "规则"}
    label = labels.get(kind, kind)
    for canonical in sorted(groups):
        variants = sorted(groups[canonical])
        mapping[canonical] = canonical
        if len(variants) > 1:
            issues.append(
                Issue(
                    f"{kind}_name_normalized",
                    f"{label}命名不一致：{', '.join(repr(v) for v in variants)} "
                    f"已按规范化名称 {canonical!r} 合并；如非同一对象请先统一命名",
                    "warning",
                    detail={"canonical": canonical, "variants": variants},
                )
            )
    return mapping, issues


def _normalize_and_deduplicate(
    observations: Iterable[RuleObservation],
) -> tuple[
    list[RuleObservation],
    list[Issue],
    dict[str, list[str]],
    dict[str, list[str]],
]:
    items = list(observations)
    issues: list[Issue] = []
    _, dataset_issues = _record_alias_groups((item.dataset for item in items), "dataset")
    _, rule_issues = _record_alias_groups((item.rule_key for item in items), "rule")
    issues.extend(dataset_issues)
    issues.extend(rule_issues)
    dataset_aliases = _collect_aliases(item.dataset for item in items)
    rule_aliases = _collect_aliases(item.rule_key for item in items)

    # Deterministic traversal order: artifact path, dataset, rule.
    ordered = sorted(
        items,
        key=lambda item: (
            item.artifact,
            normalize_name(item.dataset),
            normalize_name(item.rule_key),
            item.dataset,
            item.rule_key,
        ),
    )

    kept: dict[tuple[str, str], RuleObservation] = {}
    names: dict[str, str] = {}
    for item in ordered:
        dataset_key = normalize_name(item.dataset)
        rule_key = normalize_name(item.rule_key)
        key = (dataset_key, rule_key)
        if item.name:
            previous_name = names.get(rule_key)
            if previous_name is None:
                names[rule_key] = item.name
            elif previous_name != item.name:
                issues.append(
                    Issue(
                        "rule_name_conflict",
                        f"规则 {rule_key!r} 在不同记录中的展示名不一致："
                        f"{previous_name!r} / {item.name!r}，采用 {previous_name!r}",
                        "warning",
                        artifact=item.artifact,
                        rule=rule_key,
                        detail={"names": sorted({previous_name, item.name})},
                    )
                )
        existing = kept.get(key)
        if existing is None:
            kept[key] = RuleObservation(
                artifact=item.artifact,
                dataset=dataset_key,
                rule_key=rule_key,
                name=item.name,
                hits=item.hits,
                passes=item.passes,
                total=item.total,
            )
            continue
        if existing.counts() == item.counts():
            issues.append(
                Issue(
                    "duplicate_observation",
                    f"规则 {rule_key!r} 在数据集 {dataset_key!r} 上出现重复记录且计数一致，"
                    f"已去重（保留 {existing.artifact}）",
                    "warning",
                    artifact=item.artifact,
                    dataset=dataset_key,
                    rule=rule_key,
                )
            )
            continue
        issues.append(
            Issue(
                "conflicting_observation",
                f"规则 {rule_key!r} 在数据集 {dataset_key!r} 上出现计数冲突："
                f"{existing.artifact} 为 {existing.counts()}，"
                f"{item.artifact} 为 {item.counts()}；请先消除重复产物",
                "error",
                artifact=item.artifact,
                dataset=dataset_key,
                rule=rule_key,
            )
        )
        # Keep the first deterministic record so the result stays reproducible,
        # but the error above blocks assessment unless the caller overrides.
    final = [kept[key] for key in sorted(kept)]
    return (
        [
            RuleObservation(
                artifact=item.artifact,
                dataset=item.dataset,
                rule_key=item.rule_key,
                name=item.name or names.get(item.rule_key),
                hits=item.hits,
                passes=item.passes,
                total=item.total,
            )
            for item in final
        ],
        issues,
        dataset_aliases,
        rule_aliases,
    )
