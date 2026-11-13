"""Pre-publish impact preview.

This module is deliberately read-only: it never inserts into ``metric_versions``
or mutates the registry.  It compares a candidate definition against the
current version using:

* the stored ``definition_json`` (key, grain/unit, impl id set and per-impl
  ``source`` / ``sql_file`` fields);
* a sha256 content fingerprint of every referenced SQL file, resolved through
  the same trusted-root path helper used by validation and evaluation.

When an older stored version lacks the metadata needed for a fingerprint
comparison (missing ``source_file``, or an SQL file that no longer resolves),
the engine degrades to key + impl-id-set comparison, records a warning and
keeps working instead of raising.
"""
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import Any

from ..errors import NotFoundError, UnsafeSQLError
from ..models import ImpactPreview, ImplementDef, MetricDefinition
from .dsl_validator import resolve_sql_path
from .versioning import get_latest

# Stable machine-readable codes; both import modes share these verdicts.
RISK_IMPL_SHRINK = "impls_shrunk"
RISK_SQL_FINGERPRINT = "sql_fingerprint_changed"


def fingerprint_definition(
    metric: MetricDefinition,
    sql_roots: tuple[Path, ...] | list[Path],
) -> tuple[MetricDefinition, list[str]]:
    """Return a copy of ``metric`` with each impl's content fingerprint set.

    Fingerprints are embedded at import time so that every stored version keeps
    an immutable record of the SQL bytes it was published against. SQL that
    cannot be resolved leaves the fingerprint empty and yields a warning; the
    caller's semantic validation still reports the underlying file problem.
    """
    warnings: list[str] = []
    fingerprinted = [
        ImplementDef(
            id=impl.id,
            source=impl.source,
            sql_file=impl.sql_file,
            sql_fingerprint=impl.sql_fingerprint,
        )
        for impl in metric.impls
    ]
    for impl in fingerprinted:
        fingerprint, warning = sql_fingerprint(impl.sql_file, sql_roots)
        if warning is not None:
            warnings.append(warning)
        impl.sql_fingerprint = fingerprint or ""
    enriched = MetricDefinition(
        key=metric.key,
        name=metric.name,
        owner=metric.owner,
        description=metric.description,
        unit=metric.unit,
        grain=metric.grain,
        impls=fingerprinted,
    )
    return enriched, warnings


def _impl_index(definition: dict[str, Any]) -> dict[str, dict[str, str]]:
    return {
        str(item.get("id", "")): {
            "source": str(item.get("source", "")),
            "sql_file": str(item.get("sql_file", "")),
            "sql_fingerprint": str(item.get("sql_fingerprint", "") or ""),
        }
        for item in definition.get("impls", [])
        if isinstance(item, dict) and item.get("id")
    }


def sql_fingerprint(
    sql_file: str, sql_roots: tuple[Path, ...] | list[Path]
) -> tuple[str | None, str | None]:
    """Return ``(fingerprint, warning)`` for a relative SQL file.

    The fingerprint hashes the raw file bytes, so a content-only edit is
    detected even when the file name is unchanged.  A missing or unresolvable
    file yields ``(None, warning)`` so callers can degrade rather than fail.
    """
    if not sql_file:
        return None, f"sql_file 为空，无法计算指纹"
    try:
        path = resolve_sql_path(sql_file, sql_roots)
    except (UnsafeSQLError, NotFoundError) as exc:
        return None, f"SQL 文件无法解析，跳过指纹比对: {sql_file} ({exc})"
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        return None, f"SQL 文件无法读取，跳过指纹比对: {sql_file} ({exc})"
    return f"sha256:{digest}", None


def _affected_history(conn: sqlite3.Connection, key: str) -> tuple[list[int], int]:
    rows = conn.execute(
        "SELECT run_id FROM snapshots WHERE metric_key = ?", (key,)
    ).fetchall()
    run_ids = sorted({int(row["run_id"]) for row in rows})
    snapshot_count = int(
        conn.execute(
            "SELECT COUNT(*) FROM snapshots WHERE metric_key = ?", (key,)
        ).fetchone()[0]
    )
    return run_ids, snapshot_count


def build_impact_preview(
    conn: sqlite3.Connection,
    candidate: MetricDefinition,
    sql_roots: tuple[Path, ...] | list[Path],
    *,
    source_file: str = "<inline>",
) -> ImpactPreview:
    """Compute the read-only impact of importing ``candidate``.

    Performs only SELECT statements and file reads; safe to call on a
    read-only connection.  The same verdict is produced regardless of whether
    the caller enforces strict mode.
    """
    warnings: list[str] = []
    risk_reasons: list[str] = []

    row = conn.execute(
        "SELECT current_version FROM metrics WHERE key = ?", (candidate.key,)
    ).fetchone()

    # ---- brand-new metric: nothing historical can be affected -------------
    if row is None:
        run_ids, snapshot_count = [], 0
        return ImpactPreview(
            key=candidate.key,
            source_file=source_file,
            exists=False,
            current_version=None,
            candidate_version=1,
            changed=True,
            high_risk=False,
            warnings=warnings,
            added_impls=sorted(impl.id for impl in candidate.impls),
            removed_impls=[],
            affected_run_ids=run_ids,
            affected_snapshot_count=snapshot_count,
        )

    current_version = int(row["current_version"])
    current = get_latest(conn, candidate.key)
    current_def = current["definition"]
    degraded = not current.get("source_file")
    if degraded:
        warnings.append(
            "历史版本缺少 source_file 元数据，降级为按 key 与 impl id 集合判断"
        )

    old_impls = _impl_index(current_def)
    new_impls = _impl_index(candidate.to_dict())
    old_ids, new_ids = set(old_impls), set(new_impls)

    added = sorted(new_ids - old_ids)
    removed = sorted(old_ids - new_ids)
    shared = sorted(old_ids & new_ids)

    if removed:
        risk_reasons.append(RISK_IMPL_SHRINK)

    modified: list[dict[str, Any]] = []
    fingerprint_changes: list[dict[str, Any]] = []

    for impl_id in shared:
        old_meta, new_meta = old_impls[impl_id], new_impls[impl_id]
        field_changes: dict[str, dict[str, str]] = {}
        for field_name in ("source", "sql_file"):
            if old_meta.get(field_name) != new_meta.get(field_name):
                field_changes[field_name] = {
                    "before": old_meta.get(field_name, ""),
                    "after": new_meta.get(field_name, ""),
                }

        # Candidate (after) side always reflects the SQL bytes currently on
        # disk, resolved through the same trusted-root helper as evaluation.
        new_fp, new_warn = sql_fingerprint(new_meta["sql_file"], sql_roots)
        if new_warn and new_warn not in warnings:
            warnings.append(new_warn)

        # Historical (before) side: prefer the fingerprint frozen into the
        # stored version. Legacy rows without it fall back to reading the
        # referenced file from disk and run in degraded mode.
        old_fp = old_meta.get("sql_fingerprint") or None
        if old_fp is None:
            old_fp, old_warn = sql_fingerprint(old_meta["sql_file"], sql_roots)
            if old_warn and old_warn not in warnings:
                warnings.append(old_warn)
            degraded = True
            warnings.append(
                f"实现 {impl_id} 的历史版本缺少内嵌 SQL 指纹，"
                "降级为按 key、impl id 集合与当前磁盘文件判断"
            )

        sql_changed = False
        if old_fp is None or new_fp is None:
            # Cannot establish byte-level equivalence on either side; the
            # field-level comparison above still surfaces path/source edits.
            degraded = True
        elif old_fp != new_fp:
            sql_changed = True
            fingerprint_changes.append(
                {
                    "impl_id": impl_id,
                    "sql_file": new_meta["sql_file"],
                    "before_fingerprint": old_fp,
                    "after_fingerprint": new_fp,
                }
            )

        if field_changes or sql_changed:
            modified.append(
                {
                    "impl_id": impl_id,
                    "field_changes": field_changes,
                    "sql_fingerprint_changed": sql_changed,
                }
            )
        if sql_changed:
            risk_reasons.append(RISK_SQL_FINGERPRINT)

    grain_before = str(current_def.get("grain", ""))
    grain_after = candidate.grain
    unit_before = str(current_def.get("unit", ""))
    unit_after = candidate.unit

    changed = bool(
        added
        or removed
        or modified
        or grain_before != grain_after
        or unit_before != unit_after
    )
    high_risk = bool(risk_reasons)
    run_ids, snapshot_count = _affected_history(conn, candidate.key)

    return ImpactPreview(
        key=candidate.key,
        source_file=source_file,
        exists=True,
        current_version=current_version,
        candidate_version=current_version + 1,
        changed=changed,
        high_risk=high_risk,
        degraded=degraded,
        risk_reasons=risk_reasons,
        warnings=warnings,
        added_impls=added,
        removed_impls=removed,
        modified_impls=modified,
        sql_fingerprint_changes=fingerprint_changes,
        grain_changed=grain_before != grain_after,
        grain_before=grain_before,
        grain_after=grain_after,
        unit_changed=unit_before != unit_after,
        unit_before=unit_before,
        unit_after=unit_after,
        affected_run_ids=run_ids,
        affected_snapshot_count=snapshot_count,
    )
