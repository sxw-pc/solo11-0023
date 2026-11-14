"""Metric repository versioning and recursive field-level diffs."""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from ..errors import NotFoundError
from ..models import ImplementDef, MetricDefinition, utc_now


def _decode(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    item = dict(row)
    item["definition"] = json.loads(item.pop("definition_json"))
    return item


def canonical_definition(metric: MetricDefinition) -> str:
    """Serialize the *parsed* definition deterministically.

    Version identity is decided on this structured form, never on the raw
    YAML bytes: comments, key order and formatting do not create versions.
    """
    return json.dumps(metric.to_dict(), ensure_ascii=False, sort_keys=True)


def _definitions_equal(left_json: str, right_json: str) -> bool:
    """Compare two stored definitions field by field after parsing."""
    try:
        left = json.loads(left_json)
        right = json.loads(right_json)
    except (TypeError, ValueError):
        return left_json == right_json
    return left == right


def candidate_version(conn: sqlite3.Connection, metric: MetricDefinition) -> int:
    """Version this definition would end up at if imported now (read-only).

    Unchanged content resolves to the current version because it would not
    create a new one.
    """
    row = conn.execute(
        "SELECT current_version, definition_json FROM metrics WHERE key = ?",
        (metric.key,),
    ).fetchone()
    if row is None:
        return 1
    if _definitions_equal(row["definition_json"], canonical_definition(metric)):
        return int(row["current_version"])
    return int(row["current_version"]) + 1


def _insert_impls(
    conn: sqlite3.Connection, key: str, version: int, impls: list[ImplementDef]
) -> None:
    """Store this version's own implementation list.

    Rows are keyed by (metric_key, version): switching to a new version never
    rewrites or deletes another version's list, so history stays auditable.
    """
    conn.executemany(
        """
        INSERT INTO metric_impls(metric_key, version, impl_id, source, sql_file,
                                 position)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(metric_key, version, impl_id) DO UPDATE SET
            source=excluded.source, sql_file=excluded.sql_file,
            position=excluded.position
        """,
        [
            (key, version, impl.id, impl.source, impl.sql_file, position)
            for position, impl in enumerate(impls)
        ],
    )


def backfill_impls(conn: sqlite3.Connection, key: str | None = None) -> None:
    """Materialize per-version impl rows for repositories created before the
    table existed.  Derived from each version's immutable definition_json, so
    old artifacts keep working without wiping and re-importing anything."""
    query = (
        "SELECT v.metric_key, v.version, v.definition_json FROM metric_versions v "
        "WHERE NOT EXISTS (SELECT 1 FROM metric_impls i "
        "                  WHERE i.metric_key = v.metric_key "
        "                    AND i.version = v.version)"
    )
    params: tuple[Any, ...] = ()
    if key is not None:
        query += " AND v.metric_key = ?"
        params = (key,)
    try:
        rows = conn.execute(query, params).fetchall()
    except sqlite3.OperationalError:
        # Connection opened before the migration ran: fall back to the
        # definition_json path used by ``impls_for_version``.
        return
    for row in rows:
        try:
            definition = json.loads(row["definition_json"])
        except (TypeError, ValueError):
            continue
        impls = MetricDefinition.from_dict(definition).impls
        if impls:
            _insert_impls(conn, row["metric_key"], int(row["version"]), impls)


def _impls_from_definition(definition: dict[str, Any] | None) -> list[ImplementDef]:
    return MetricDefinition.from_dict(definition or {}).impls


def impls_for_version(
    conn: sqlite3.Connection, key: str, version: int
) -> list[ImplementDef]:
    """Implementation list declared by one specific metric version.

    Prefers the normalized per-version rows; falls back to that version's own
    definition_json for repositories that predate ``metric_impls``.  It never
    merges implementations across versions.
    """
    try:
        rows = conn.execute(
            "SELECT impl_id, source, sql_file FROM metric_impls "
            "WHERE metric_key = ? AND version = ? ORDER BY position, id",
            (key, int(version)),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    if rows:
        return [
            ImplementDef(id=row["impl_id"], source=row["source"],
                         sql_file=row["sql_file"])
            for row in rows
        ]
    stored = conn.execute(
        "SELECT definition_json FROM metric_versions "
        "WHERE metric_key = ? AND version = ?",
        (key, int(version)),
    ).fetchone()
    if stored is None:
        return []
    try:
        definition = json.loads(stored["definition_json"])
    except (TypeError, ValueError):
        return []
    return _impls_from_definition(definition)


def append_version(
    conn: sqlite3.Connection,
    metric: MetricDefinition,
    source_file: str = "<inline>",
) -> int:
    """Insert an immutable version and update the latest pointer atomically.

    Re-importing byte-for-byte different YAML that parses to the same
    structured definition is a no-op: ``current_version`` is kept and no row
    is appended.  Only a real definition change creates a new version.
    """
    now = utc_now()
    definition_json = canonical_definition(metric)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT current_version, definition_json FROM metrics WHERE key = ?",
            (metric.key,),
        ).fetchone()
        if row is not None and _definitions_equal(
            row["definition_json"], definition_json
        ):
            # Unchanged content: refresh the per-version impl rows for legacy
            # repositories, but keep the version pointer and history untouched.
            backfill_impls(conn, metric.key)
            conn.commit()
            return int(row["current_version"])
        version = int(row["current_version"]) + 1 if row else 1
        # Materialize impl rows for pre-existing versions (old repositories)
        # before adding the new one, so every historical version keeps its own
        # list and none of them is derived from the new definition.
        backfill_impls(conn, metric.key)
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
        _insert_impls(conn, metric.key, version, metric.impls)
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
