"""YAML metric-definition parser with field-level diagnostics.

支持两个规则格式版本（对应底层质量库 dqkit 的两个大版本）：

* ``schema: 1``（或省略）——旧版格式：实现字段为 ``source`` / ``sql_file``，
  历史规则文件原样可解析，不需要任何修改。
* ``schema: 2``——新版格式：实现字段为 ``dataset`` / ``query``，可选
  ``quality: {engine, api}`` 块声明质量引擎版本要求；导入时会校验该
  版本钉（pin）与当前安装的 dqkit 是否匹配。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import yaml

from ..errors import DslParseError
from ..models import ImplementDef, MetricDefinition, ValidationIssue

TOP_FIELDS = {"key", "name", "owner", "description", "unit", "grain", "impls"}
TOP_FIELDS_V1 = TOP_FIELDS | {"schema"}
TOP_FIELDS_V2 = TOP_FIELDS | {"schema", "quality"}
IMPL_FIELDS = {"id", "source", "sql_file"}
IMPL_FIELDS_V2 = {"id", "dataset", "query"}
QUALITY_FIELDS = {"engine", "api"}
SUPPORTED_SCHEMA_VERSIONS = (1, 2)


def _issue(field: str, code: str, message: str) -> ValidationIssue:
    return ValidationIssue(field=field, code=code, message=message)


def _parse_schema_version(raw: Mapping[str, Any]) -> int:
    value = raw.get("schema", 1)
    if isinstance(value, bool) or not isinstance(value, int):
        raise DslParseError(
            f"schema 必须是整数（当前支持: {SUPPORTED_SCHEMA_VERSIONS}），实际: {value!r}",
            code="unsupported_schema_version",
        )
    if value not in SUPPORTED_SCHEMA_VERSIONS:
        raise DslParseError(
            f"不支持的规则格式版本 schema={value}；当前 caliber 支持 "
            f"1（旧版 dqkit 格式）与 2（dqkit 2.x 格式）。"
            "请升级 caliber，或将规则文件回退到受支持的格式版本。",
            details={"schema": value, "supported": list(SUPPORTED_SCHEMA_VERSIONS)},
            code="unsupported_schema_version",
        )
    return value


def _parse_impl_items(
    raw: Any,
    issues: list[ValidationIssue],
    *,
    allowed_fields: set[str],
    field_map: dict[str, str],
) -> list[ImplementDef]:
    if not isinstance(raw, list):
        issues.append(_issue("impls", "type", "impls 必须是列表"))
        return []
    result: list[ImplementDef] = []
    for index, item in enumerate(raw):
        field_name = f"impls[{index}]"
        if not isinstance(item, Mapping):
            issues.append(_issue(field_name, "type", "实现项必须是映射"))
            continue
        unknown = sorted(set(item) - allowed_fields)
        if unknown:
            issues.append(_issue(field_name, "unknown", f"未知字段: {', '.join(unknown)}"))
        values: dict[str, str] = {}
        for external, internal in field_map.items():
            value = item.get(external)
            if not isinstance(value, str) or not value.strip():
                issues.append(
                    _issue(f"{field_name}.{external}", "required", f"{external} 不能为空")
                )
                values[internal] = ""
            else:
                values[internal] = value.strip()
        result.append(
            ImplementDef(id=values["id"], source=values["source"], sql_file=values["sql_file"])
        )
    return result


def _parse_quality(raw: Any, issues: list[ValidationIssue]) -> dict[str, Any] | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        issues.append(_issue("quality", "type", "quality 必须是映射"))
        return None
    unknown = sorted(set(raw) - QUALITY_FIELDS)
    if unknown:
        issues.append(_issue("quality", "unknown", f"未知字段: {', '.join(unknown)}"))
    engine = raw.get("engine", "dqkit")
    if not isinstance(engine, str) or not engine.strip():
        issues.append(_issue("quality.engine", "type", "engine 必须是非空字符串"))
        engine = "dqkit"
    api = raw.get("api")
    if api is not None and (isinstance(api, bool) or not isinstance(api, int)):
        issues.append(_issue("quality.api", "type", "api 必须是整数"))
        api = None
    return {"engine": engine.strip(), "api": api}


def parse_metric_text(text: str, source: str = "<string>") -> MetricDefinition:
    """Parse one YAML document into a definition.

    Syntax and shape errors are collected rather than raised one by one, which
    makes the CLI/UI error output actionable.
    """
    if not text.strip():
        raise DslParseError("指标文件为空", details={"source": source})
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        location = f" 第 {mark.line + 1} 行" if mark is not None else ""
        raise DslParseError(
            f"YAML 语法错误{location}: {getattr(exc, 'problem', str(exc))}",
            details={"source": source},
            code="yaml_syntax_error",
        ) from exc

    issues: list[ValidationIssue] = []
    if not isinstance(raw, Mapping):
        raise DslParseError(
            "指标文件根节点必须是映射",
            details=[_issue("$", "type", "根节点必须是 YAML 映射").to_dict()],
        )
    schema_version = _parse_schema_version(raw)
    allowed_top = TOP_FIELDS_V1 if schema_version == 1 else TOP_FIELDS_V2
    unknown = sorted(set(raw) - allowed_top)
    if unknown:
        hint = "（schema: 2 规则请使用 dataset/query 与 quality 块）" if schema_version == 1 else ""
        issues.append(_issue("$", "unknown", f"未知顶层字段: {', '.join(unknown)}{hint}"))

    values: dict[str, str] = {}
    for name in ("key", "name", "owner", "description", "unit", "grain"):
        value = raw.get(name)
        if not isinstance(value, str):
            issues.append(_issue(name, "required", f"{name} 必须是非空字符串"))
            values[name] = ""
        else:
            values[name] = value.strip()
            if name != "description" and not values[name]:
                issues.append(_issue(name, "required", f"{name} 不能为空"))

    if schema_version == 1:
        impls = _parse_impl_items(
            raw.get("impls"),
            issues,
            allowed_fields=IMPL_FIELDS,
            field_map={"id": "id", "source": "source", "sql_file": "sql_file"},
        )
        quality = None
    else:
        impls = _parse_impl_items(
            raw.get("impls"),
            issues,
            allowed_fields=IMPL_FIELDS_V2,
            field_map={"id": "id", "dataset": "source", "query": "sql_file"},
        )
        quality = _parse_quality(raw.get("quality"), issues)

    if issues:
        raise DslParseError(
            "指标 YAML 字段不完整或类型错误",
            details=[issue.to_dict() for issue in issues],
            code="dsl_field_error",
        )
    return MetricDefinition(
        key=values["key"],
        name=values["name"],
        owner=values["owner"],
        description=values["description"],
        unit=values["unit"],
        grain=values["grain"],
        impls=impls,
        schema_version=schema_version,
        quality=quality,
    )


def parse_metric_file(path: str | Path) -> MetricDefinition:
    file_path = Path(path)
    try:
        text = file_path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise DslParseError(f"无法以 UTF-8 读取文件: {file_path}") from exc
    except OSError as exc:
        raise DslParseError(f"无法读取指标文件: {file_path}: {exc}") from exc
    return parse_metric_text(text, source=str(file_path))
