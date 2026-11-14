from __future__ import annotations

import sqlite3
from pathlib import Path

from app.core.snapshot import get_run
from app.db import init_db


def test_legacy_database_without_manifest_column_migrates(tmp_path: Path):
    db_path = tmp_path / "legacy.sqlite3"
    # Hand-build a repository with the pre-migration runs schema.
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE runs ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, status TEXT NOT NULL, "
            "started_at TEXT NOT NULL, finished_at TEXT, metrics_json TEXT NOT NULL, "
            "datasets_dir TEXT NOT NULL, tolerances_json TEXT NOT NULL, "
            "summary_json TEXT NOT NULL DEFAULT '{}', error TEXT)"
        )
        conn.execute(
            "INSERT INTO runs(status, started_at, metrics_json, datasets_dir, "
            "tolerances_json, summary_json) VALUES "
            "('completed', '2026-01-01T00:00:00+00:00', '[]', '/data', '{}', '{}')"
        )
        conn.commit()
    finally:
        conn.close()

    migrated = init_db(db_path)
    try:
        columns = {
            row[1] for row in migrated.execute("PRAGMA table_info(runs)").fetchall()
        }
        assert "datasets_manifest_json" in columns
        run = get_run(migrated, 1)
        assert run["datasets_manifest"] == []
    finally:
        migrated.close()

    # Migration is idempotent on the next open.
    again = init_db(db_path)
    try:
        assert get_run(again, 1)["status"] == "completed"
    finally:
        again.close()
