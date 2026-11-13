"""Metric repository versioning and recursive field-level diffs."""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from ..audit import insert_audit_row, record_error
from ..errors import CaliberError, NotFoundError
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
        """
        SELECT v.metric_key AS key, v.version, v.definition_json, v.source_file,
               v.created_at, r.kind AS origin, r.id AS rollback_event_id
        FROM metric_versions v
        LEFT JOIN metric_rollbacks r
          ON r.metric_key = v.metric_key AND r.new_version = v.version
        WHERE v.metric_key = ? ORDER BY v.version DESC
        """,
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


def _decode_rollback(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def rollback_to_version(
    conn: sqlite3.Connection,
    key: str,
    target_version: int,
    reason: str = "",
    *,
    kind: str = "rollback",
    undo_of: int | None = None,
) -> dict[str, Any]:
    """Roll a metric back to a historical version, append-only and atomic.

    The rollback never rewrites existing rows: it copies the target version's
    definition into a NEW version number and moves the ``current_version``
    pointer, so a version number always refers to exactly one definition.
    The version row, pointer update, rollback event and audit record commit
    in a single transaction — a failure leaves the repository untouched.
    """
    target = int(target_version)
    now = utc_now()
    try:
        conn.execute("BEGIN IMMEDIATE")
        metric_row = conn.execute(
            "SELECT current_version FROM metrics WHERE key = ?", (key,)
        ).fetchone()
        if metric_row is None:
            raise NotFoundError(f"指标不存在: {key}")
        current = int(metric_row["current_version"])
        if target == current:
            raise CaliberError(
                f"指标 {key} 当前已是 v{target}，无需回退", code="rollback_noop"
            )
        version_row = conn.execute(
            "SELECT definition_json FROM metric_versions "
            "WHERE metric_key = ? AND version = ?",
            (key, target),
        ).fetchone()
        if version_row is None:
            raise NotFoundError(f"指标版本不存在: {key} v{target}")
        definition_json = version_row["definition_json"]
        definition = json.loads(definition_json)
        new_version = int(
            conn.execute(
                "SELECT COALESCE(MAX(version), 0) FROM metric_versions "
                "WHERE metric_key = ?",
                (key,),
            ).fetchone()[0]
        ) + 1
        conn.execute(
            """
            UPDATE metrics SET name = ?, owner = ?, unit = ?, grain = ?,
                current_version = ?, definition_json = ?, updated_at = ?
            WHERE key = ?
            """,
            (
                str(definition.get("name", key)),
                str(definition.get("owner", "")),
                str(definition.get("unit", "")),
                str(definition.get("grain", "")),
                new_version,
                definition_json,
                now,
                key,
            ),
        )
        conn.execute(
            "INSERT INTO metric_versions(metric_key, version, definition_json, "
            "source_file, created_at) VALUES (?, ?, ?, ?, ?)",
            (key, new_version, definition_json, f"{kind}:v{target}", now),
        )
        cursor = conn.execute(
            "INSERT INTO metric_rollbacks(metric_key, kind, from_version, "
            "to_version, new_version, reason, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (key, kind, current, target, new_version, reason or "", now),
        )
        event_id = int(cursor.lastrowid or 0)
        if undo_of is not None:
            conn.execute(
                "UPDATE metric_rollbacks SET undone_at = ?, undone_by = ? "
                "WHERE id = ?",
                (now, event_id, int(undo_of)),
            )
        insert_audit_row(
            conn,
            "metric.rollback" if kind == "rollback" else "metric.rollback_undo",
            key,
            "ok",
            {
                "event_id": event_id,
                "from_version": current,
                "to_version": target,
                "new_version": new_version,
                "reason": reason or "",
                **({"undo_of": int(undo_of)} if undo_of is not None else {}),
            },
        )
        conn.commit()
    except Exception as exc:
        conn.rollback()
        if not isinstance(exc, CaliberError):
            record_error(
                conn,
                "metric.rollback" if kind == "rollback" else "metric.rollback_undo",
                key,
                exc,
            )
        raise
    event = _decode_rollback(
        conn.execute(
            "SELECT * FROM metric_rollbacks WHERE id = ?", (event_id,)
        ).fetchone()
    )
    return {"event": event, "metric": get_latest(conn, key)}


def undo_rollback(
    conn: sqlite3.Connection, key: str, reason: str = ""
) -> dict[str, Any]:
    """Undo the most recent not-yet-undone rollback of a metric.

    The undo is itself an append-only rollback to the version that was
    current before the rollback being undone, so history is never rewritten.
    """
    event = _decode_rollback(
        conn.execute(
            "SELECT * FROM metric_rollbacks WHERE metric_key = ? "
            "AND kind = 'rollback' AND undone_at IS NULL ORDER BY id DESC LIMIT 1",
            (key,),
        ).fetchone()
    )
    if event is None:
        # Distinguish "metric missing" from "nothing to undo".
        get_latest(conn, key)
        raise CaliberError(
            f"指标 {key} 没有可撤销的回退记录", code="rollback_nothing_to_undo"
        )
    return rollback_to_version(
        conn,
        key,
        int(event["from_version"]),
        reason or f"撤销回退事件 #{event['id']}（v{event['from_version']} → "
        f"v{event['to_version']}）",
        kind="undo",
        undo_of=int(event["id"]),
    )


def list_rollbacks(conn: sqlite3.Connection, key: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM metric_rollbacks WHERE metric_key = ? ORDER BY id DESC",
        (key,),
    ).fetchall()
    return [dict(row) for row in rows]


def rollback_event_for_version(
    conn: sqlite3.Connection, key: str, version: int
) -> dict[str, Any] | None:
    """Return the rollback/undo event that created ``version``, if any."""
    return _decode_rollback(
        conn.execute(
            "SELECT * FROM metric_rollbacks WHERE metric_key = ? "
            "AND new_version = ? ORDER BY id DESC LIMIT 1",
            (key, int(version)),
        ).fetchone()
    )


def latest_active_rollback(
    conn: sqlite3.Connection, key: str
) -> dict[str, Any] | None:
    """Return the latest rollback event that has not been undone."""
    return _decode_rollback(
        conn.execute(
            "SELECT * FROM metric_rollbacks WHERE metric_key = ? "
            "AND kind = 'rollback' AND undone_at IS NULL ORDER BY id DESC LIMIT 1",
            (key,),
        ).fetchone()
    )
