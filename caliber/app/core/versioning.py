"""Metric repository versioning and recursive field-level diffs."""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from ..errors import NotFoundError
from ..models import MetricDefinition, utc_now


def _decode(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    item = dict(row)
    item["definition"] = json.loads(item.pop("definition_json"))
    return item


def append_version(
    conn: sqlite3.Connection,
    metric: MetricDefinition,
    source_file: str = "<inline>",
) -> int:
    """Insert an immutable version and update the latest pointer atomically."""
    now = utc_now()
    definition_json = json.dumps(metric.to_dict(), ensure_ascii=False, sort_keys=True)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT current_version FROM metrics WHERE key = ?", (metric.key,)
        ).fetchone()
        version = int(row["current_version"]) + 1 if row else 1
        conn.execute(
            """
            INSERT INTO metrics(key, name, owner, unit, grain, current_version,
                                definition_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                name=excluded.name, owner=excluded.owner, unit=excluded.unit,
                grain=excluded.grain, current_version=excluded.current_version,
                definition_json=excluded.definition_json, updated_at=excluded.updated_at
            """,
            (metric.key, metric.name, metric.owner, metric.unit, metric.grain,
             version, definition_json, now, now),
        )
        conn.execute(
            "INSERT INTO metric_versions(metric_key, version, definition_json, "
            "source_file, created_at) VALUES (?, ?, ?, ?, ?)",
            (metric.key, version, definition_json, source_file, now),
        )
        conn.commit()
        return version
    except Exception:
        conn.rollback()
        raise


def get_latest(conn: sqlite3.Connection, key: str) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT m.key, m.name, m.owner, m.unit, m.grain, m.current_version AS version,
               m.updated_at, v.definition_json, v.source_file, v.created_at
        FROM metrics m JOIN metric_versions v
          ON v.metric_key = m.key AND v.version = m.current_version
        WHERE m.key = ?
        """,
        (key,),
    ).fetchone()
    result = _decode(row)
    if result is None:
        raise NotFoundError(f"指标不存在: {key}")
    return result


def get_version(conn: sqlite3.Connection, key: str, version: int) -> dict[str, Any]:
    row = conn.execute(
        "SELECT metric_key AS key, version, definition_json, source_file, created_at "
        "FROM metric_versions WHERE metric_key = ? AND version = ?",
        (key, int(version)),
    ).fetchone()
    result = _decode(row)
    if result is None:
        raise NotFoundError(f"指标版本不存在: {key} v{version}")
    return result


def list_versions(conn: sqlite3.Connection, key: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT metric_key AS key, version, definition_json, source_file, created_at "
        "FROM metric_versions WHERE metric_key = ? ORDER BY version DESC",
        (key,),
    ).fetchall()
    return [_decode(row) or {} for row in rows]


def list_metrics(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT key, name, owner, unit, grain, current_version AS version, updated_at "
        "FROM metrics ORDER BY key"
    ).fetchall()
    return [dict(row) for row in rows]


def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    flat: dict[str, Any] = {}
    if isinstance(value, dict):
        for key in sorted(value):
            child = f"{prefix}.{key}" if prefix else str(key)
            flat.update(_flatten(value[key], child))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            flat.update(_flatten(item, f"{prefix}[{index}]"))
    else:
        flat[prefix] = value
    return flat


def diff_versions(
    conn: sqlite3.Connection, key: str, from_version: int, to_version: int
) -> dict[str, Any]:
    before = get_version(conn, key, from_version)["definition"]
    after = get_version(conn, key, to_version)["definition"]
    old_flat, new_flat = _flatten(before), _flatten(after)
    added = [
        {"field": name, "after": new_flat[name]}
        for name in sorted(set(new_flat) - set(old_flat))
    ]
    removed = [
        {"field": name, "before": old_flat[name]}
        for name in sorted(set(old_flat) - set(new_flat))
    ]
    modified = [
        {"field": name, "before": old_flat[name], "after": new_flat[name]}
        for name in sorted(set(old_flat) & set(new_flat))
        if old_flat[name] != new_flat[name]
    ]
    return {
        "key": key,
        "from_version": int(from_version),
        "to_version": int(to_version),
        "added": added,
        "removed": removed,
        "modified": modified,
        "changed": bool(added or removed or modified),
    }
