"""Append-only audit trail helpers."""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from .models import utc_now


def _encode(detail: Any) -> str:
    return json.dumps(detail or {}, ensure_ascii=False, default=str)


def insert_audit_row(
    conn: sqlite3.Connection,
    action: str,
    entity: str,
    status: str = "ok",
    detail: dict[str, Any] | None = None,
) -> int:
    """Insert one audit row WITHOUT committing.

    Intended for callers that manage their own transaction (e.g. rollback),
    so the audit record commits or rolls back together with the mutation.
    """
    cursor = conn.execute(
        "INSERT INTO audit_log(created_at, action, entity, status, detail_json) "
        "VALUES (?, ?, ?, ?, ?)",
        (utc_now(), action, entity, status, _encode(detail)),
    )
    return int(cursor.lastrowid or 0)


def log_audit(
    conn: sqlite3.Connection,
    action: str,
    entity: str,
    status: str = "ok",
    detail: dict[str, Any] | None = None,
) -> int:
    row_id = insert_audit_row(conn, action, entity, status, detail)
    conn.commit()
    return row_id


def record_error(
    conn: sqlite3.Connection,
    action: str,
    entity: str,
    error: Exception,
    *,
    extra: dict[str, Any] | None = None,
) -> int:
    detail: dict[str, Any] = {"error": type(error).__name__, "message": str(error)}
    if extra:
        detail.update(extra)
    return log_audit(conn, action, entity, "error", detail)


def list_audit(conn: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, created_at, action, entity, status, detail_json "
        "FROM audit_log ORDER BY id DESC LIMIT ?",
        (max(1, int(limit)),),
    ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        try:
            item["detail"] = json.loads(item.pop("detail_json"))
        except json.JSONDecodeError:
            item["detail"] = {"raw": item.pop("detail_json")}
        result.append(item)
    return result
