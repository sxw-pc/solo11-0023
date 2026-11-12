"""YAML metric-definition parser with field-level diagnostics."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import yaml

from ..errors import DslParseError
from ..models import ImplementDef, MetricDefinition, ValidationIssue

TOP_FIELDS = {"key", "name", "owner", "description", "unit", "grain", "impls"}
IMPL_FIELDS = {"id", "source", "sql_file"}


def _issue(field: str, code: str, message: str) -> ValidationIssue:
    return ValidationIssue(field=field, code=code, message=message)


def _parse_impls(raw: Any, issues: list[ValidationIssue]) -> list[ImplementDef]:
    if not isinstance(raw, list):
        issues.append(_issue("impls", "type", "impls 必须是列表"))
        return []
    result: list[ImplementDef] = []
    for index, item in enumerate(raw):
        field_name = f"impls[{index}]"
        if not isinstance(item, Mapping):
            issues.append(_issue(field_name, "type", "实现项必须是映射"))
            continue
        unknown = sorted(set(item) - IMPL_FIELDS)
        if unknown:
            issues.append(_issue(field_name, "unknown", f"未知字段: {', '.join(unknown)}"))
        values: dict[str, str] = {}
        for name in ("id", "source", "sql_file"):
            value = item.get(name)
            if not isinstance(value, str) or not value.strip():
                issues.append(_issue(f"{field_name}.{name}", "required", f"{name} 不能为空"))
                values[name] = ""
            else:
                values[name] = value.strip()
        result.append(
            ImplementDef(id=values["id"], source=values["source"], sql_file=values["sql_file"])
        )
    return result


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
    unknown = sorted(set(raw) - TOP_FIELDS)
    if unknown:
        issues.append(_issue("$", "unknown", f"未知顶层字段: {', '.join(unknown)}"))

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
    impls = _parse_impls(raw.get("impls"), issues)

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
