"""Semantic validation and SQL path confinement for metric definitions."""
from __future__ import annotations

import re
from pathlib import Path

from ..errors import NotFoundError, UnsafeSQLError, ValidationError
from ..models import MetricDefinition, ValidationIssue

KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{2,40}$")
IMPL_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
AXIS_PATTERN = re.compile(r"^[A-Za-z_一-鿿][0-9A-Za-z_一-鿿]*$")
ALLOWED_GRAINS = {"day", "week", "month"}


def resolve_sql_path(sql_file: str, sql_roots: tuple[Path, ...] | list[Path]) -> Path:
    """Resolve a relative SQL file inside one of the trusted roots.

    Absolute paths, ``..`` components and symlinks escaping a trusted root are
    rejected.  The returned path is always an existing regular file.
    """
    raw = Path(sql_file)
    if not sql_file or raw.is_absolute() or raw.drive or raw.anchor:
        raise UnsafeSQLError("sql_file 必须是受信任目录内的相对路径")
    if any(part == ".." for part in raw.parts):
        raise UnsafeSQLError("sql_file 不允许包含 .. 路径片段")
    for root in sql_roots:
        root_path = Path(root).resolve()
        candidate = (root_path / raw).resolve()
        if not candidate.is_relative_to(root_path):
            continue
        if candidate.is_file():
            return candidate
    raise NotFoundError(f"受信任 SQL 目录中不存在文件: {sql_file}")


def validate_definition(
    metric: MetricDefinition,
    sql_roots: tuple[Path, ...] | list[Path],
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []

    def add(field: str, code: str, message: str) -> None:
        issues.append(ValidationIssue(field=field, code=code, message=message))

    if not KEY_PATTERN.fullmatch(metric.key or ""):
        add("key", "pattern", "key 必须匹配 ^[a-z][a-z0-9_]{2,40}$")
    if not metric.name.strip():
        add("name", "required", "name 不能为空")
    if not metric.owner.strip():
        add("owner", "required", "owner 不能为空")
    if not metric.unit.strip():
        add("unit", "required", "unit 不能为空")
    if metric.grain not in ALLOWED_GRAINS:
        add("grain", "enum", "grain 必须是 day/week/month 之一")
    seen_axes: set[str] = set()
    for index, axis in enumerate(metric.grain_axes):
        field_name = f"grain_axes[{index}]"
        if not AXIS_PATTERN.fullmatch(axis or ""):
            add(field_name, "pattern", "粒度轴列名只能包含字母、数字、下划线或中文，且不能以数字开头")
        elif axis in seen_axes:
            add(field_name, "duplicate", f"粒度轴列名重复: {axis}")
        seen_axes.add(axis)
    if len(metric.impls) < 2:
        add("impls", "min_items", "至少需要两个实现才能进行一致性比对")

    seen: set[str] = set()
    roots = tuple(Path(root) for root in sql_roots)
    if not roots:
        add("impls", "no_sql_roots", "未配置受信任的 SQL 文件目录")
    for index, impl in enumerate(metric.impls):
        prefix = f"impls[{index}]"
        if not IMPL_ID_PATTERN.fullmatch(impl.id or ""):
            add(f"{prefix}.id", "pattern", "实现 id 必须匹配 ^[a-z][a-z0-9_]{1,40}$")
        if impl.id in seen:
            add(f"{prefix}.id", "duplicate", f"实现 id 重复: {impl.id}")
        seen.add(impl.id)
        if not impl.source.strip():
            add(f"{prefix}.source", "required", "source 不能为空")
        try:
            resolve_sql_path(impl.sql_file, roots)
        except UnsafeSQLError as exc:
            add(f"{prefix}.sql_file", "unsafe_path", str(exc))
        except NotFoundError as exc:
            add(f"{prefix}.sql_file", "missing_file", str(exc))
    return issues


def assert_valid(
    metric: MetricDefinition,
    sql_roots: tuple[Path, ...] | list[Path],
) -> None:
    issues = validate_definition(metric, sql_roots)
    if issues:
        raise ValidationError([issue.to_dict() for issue in issues])
