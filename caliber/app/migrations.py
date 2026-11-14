"""Schema baseline and idempotent, transactional, audited migrations.

The database schema is versioned with ``PRAGMA user_version``:

* **v1 — baseline**: the original tables (metrics, metric_versions, runs,
  snapshots, audit_log).  Pre-versioning databases created by older builds
  are *adopted* as v1: the baseline statements all use ``IF NOT EXISTS``,
  so applying them to a legacy file is a no-op that only stamps the version.
* **v2 — snapshot projection columns**: adds ``dimension_count`` /
  ``diff_count`` derived columns plus verdict indexes to ``snapshots`` and
  backfills them from ``summary_json``.  The JSON payload columns are
  deliberately *not* split (see docs/migrations.md for the rationale).

Each migration runs inside a single ``BEGIN IMMEDIATE`` transaction that
covers the DDL, the data backfill, the ``user_version`` stamp and the
``db.migrated`` audit record — a failure rolls everything back, leaving no
half-migrated state.  Migrations are skipped when ``user_version`` already
covers them, and the audit insert is additionally deduplicated by entity,
so running the migration any number of times never produces duplicate
audit rows.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Callable

from .errors import MigrationError
from .models import utc_now

SCHEMA_VERSION = 2

#: v1 baseline as individual statements.  They are executed one by one
#: inside the migration transaction; ``executescript`` is avoided on
#: purpose because it commits any open transaction before running.
BASELINE_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS metrics (
        key TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        owner TEXT NOT NULL,
        unit TEXT NOT NULL,
        grain TEXT NOT NULL,
        current_version INTEGER NOT NULL,
        definition_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS metric_versions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        metric_key TEXT NOT NULL REFERENCES metrics(key) ON DELETE CASCADE,
        version INTEGER NOT NULL,
        definition_json TEXT NOT NULL,
        source_file TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(metric_key, version)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        status TEXT NOT NULL,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        metrics_json TEXT NOT NULL,
        datasets_dir TEXT NOT NULL,
        tolerances_json TEXT NOT NULL,
        summary_json TEXT NOT NULL DEFAULT '{}',
        error TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
        metric_key TEXT NOT NULL,
        metric_version INTEGER NOT NULL,
        datasets_json TEXT NOT NULL,
        impl_ids_json TEXT NOT NULL,
        verdict TEXT NOT NULL,
        summary_json TEXT NOT NULL,
        result_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL,
        action TEXT NOT NULL,
        entity TEXT NOT NULL,
        status TEXT NOT NULL,
        detail_json TEXT NOT NULL DEFAULT '{}'
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_versions_key ON metric_versions(metric_key, version DESC)",
    "CREATE INDEX IF NOT EXISTS idx_snapshots_key ON snapshots(metric_key, id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_snapshots_run ON snapshots(run_id)",
    "CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at DESC)",
)


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    apply: Callable[[sqlite3.Connection], dict[str, Any]]


def current_schema_version(conn: sqlite3.Connection) -> int:
    """Return the schema version stamped in ``PRAGMA user_version``."""
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def _set_schema_version(conn: sqlite3.Connection, version: int) -> None:
    conn.execute(f"PRAGMA user_version = {int(version)}")


def _table_names(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [str(row[0]) for row in rows]


def _has_tables(conn: sqlite3.Connection) -> bool:
    return bool(_table_names(conn))


def _record_migration_audit(
    conn: sqlite3.Connection,
    migration: Migration,
    from_version: int,
    detail: dict[str, Any],
) -> None:
    """Insert the ``db.migrated`` audit row inside the migration transaction.

    Deduplicated by entity: if a record for this schema version already
    exists (e.g. stamped manually or by a previous crashed-then-recovered
    attempt), no second row is written.
    """
    entity = f"schema:v{migration.version}"
    exists = conn.execute(
        "SELECT 1 FROM audit_log WHERE action = 'db.migrated' AND entity = ? LIMIT 1",
        (entity,),
    ).fetchone()
    if exists:
        return
    payload = {
        "from_version": from_version,
        "to_version": migration.version,
        "migration": migration.name,
        **detail,
    }
    conn.execute(
        "INSERT INTO audit_log(created_at, action, entity, status, detail_json) "
        "VALUES (?, ?, ?, ?, ?)",
        (utc_now(), "db.migrated", entity, "ok",
         json.dumps(payload, ensure_ascii=False, default=str)),
    )


def _apply_baseline(conn: sqlite3.Connection) -> dict[str, Any]:
    detected = "legacy" if _has_tables(conn) else "fresh"
    for statement in BASELINE_STATEMENTS:
        conn.execute(statement)
    return {"detected": detected, "tables": _table_names(conn)}


def _apply_snapshot_projection(conn: sqlite3.Connection) -> dict[str, Any]:
    columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(snapshots)").fetchall()
    }
    added: list[str] = []
    for column in ("dimension_count", "diff_count"):
        if column not in columns:
            conn.execute(
                f"ALTER TABLE snapshots ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0"
            )
            added.append(column)
    rows = conn.execute("SELECT id, summary_json FROM snapshots").fetchall()
    for row in rows:
        try:
            summary = json.loads(row[1] or "{}")
        except json.JSONDecodeError as exc:
            raise MigrationError(
                f"快照 {row[0]} 的 summary_json 无法解析，迁移中止并已回滚",
                details={"snapshot_id": int(row[0]), "error": str(exc)},
            ) from exc
        conn.execute(
            "UPDATE snapshots SET dimension_count = ?, diff_count = ? WHERE id = ?",
            (
                int(summary.get("dimension_count") or 0),
                int(summary.get("diff_count") or 0),
                int(row[0]),
            ),
        )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_snapshots_verdict ON snapshots(verdict)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_snapshots_key_verdict "
        "ON snapshots(metric_key, verdict)"
    )
    return {"added_columns": added, "backfilled_rows": len(rows)}


MIGRATIONS: tuple[Migration, ...] = (
    Migration(version=1, name="baseline", apply=_apply_baseline),
    Migration(version=2, name="snapshot-projection-columns", apply=_apply_snapshot_projection),
)


def migrate(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Apply pending migrations in order; return the steps that ran.

    Idempotent: a database already at :data:`SCHEMA_VERSION` applies nothing
    and writes no audit rows.  A failing migration is rolled back in full —
    DDL, backfill, version stamp and audit record alike — and re-raised as
    :class:`MigrationError`, so the file is never left half-migrated.
    """
    applied: list[dict[str, Any]] = []
    for migration in MIGRATIONS:
        from_version = current_schema_version(conn)
        if from_version >= migration.version:
            continue
        try:
            conn.execute("BEGIN IMMEDIATE")
            detail = migration.apply(conn) or {}
            _set_schema_version(conn, migration.version)
            _record_migration_audit(conn, migration, from_version, detail)
            conn.commit()
        except Exception as exc:
            conn.rollback()
            if isinstance(exc, MigrationError):
                raise
            raise MigrationError(
                f"schema 迁移 v{from_version} → v{migration.version}"
                f"（{migration.name}）失败，已完整回滚: {exc}",
                details={
                    "from_version": from_version,
                    "to_version": migration.version,
                    "migration": migration.name,
                    "error": str(exc),
                },
            ) from exc
        applied.append(
            {
                "version": migration.version,
                "name": migration.name,
                "from_version": from_version,
                "detail": detail,
            }
        )
    return applied
