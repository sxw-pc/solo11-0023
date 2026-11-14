"""SQLite repository bootstrap: engine gating, connections and migrations."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from .engine import DEFAULT_MIN_SQLITE_VERSION, ensure_min_engine_version
from .migrations import BASELINE_STATEMENTS, migrate

#: Legacy single-string view of the v1 baseline, kept for compatibility.
#: New code should rely on ``app.migrations`` instead of executing this.
SCHEMA = "PRAGMA foreign_keys = ON;\n" + ";\n".join(BASELINE_STATEMENTS) + ";\n"


def connect(
    db_path: str | Path,
    *,
    min_sqlite_version: str | None = None,
) -> sqlite3.Connection:
    """Open a read/write connection after enforcing the engine version floor.

    The floor defaults to ``DEFAULT_MIN_SQLITE_VERSION`` and can be lowered
    or raised per deployment (see ``Config.min_sqlite_version`` /
    ``CALIBER_MIN_SQLITE_VERSION``).  Environments below the floor are
    rejected with :class:`app.errors.DatabaseVersionError` naming both the
    detected and the required version.
    """
    path = str(db_path)
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        ensure_min_engine_version(conn, min_sqlite_version or DEFAULT_MIN_SQLITE_VERSION)
        conn.execute("PRAGMA foreign_keys = ON")
    except Exception:
        conn.close()
        raise
    return conn


def init_db(
    db_path: str | Path,
    *,
    min_sqlite_version: str | None = None,
) -> sqlite3.Connection:
    """Open the repository and apply any pending schema migrations."""
    conn = connect(db_path, min_sqlite_version=min_sqlite_version)
    try:
        migrate(conn)
    except Exception:
        conn.close()
        raise
    return conn


def table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    tables = ("metrics", "metric_versions", "runs", "snapshots", "audit_log")
    return {name: int(conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0])
            for name in tables}
