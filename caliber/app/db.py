"""SQLite repository bootstrap and small connection helpers."""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
PRAGMA foreign_keys = ON;

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
);

CREATE TABLE IF NOT EXISTS metric_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    metric_key TEXT NOT NULL REFERENCES metrics(key) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    definition_json TEXT NOT NULL,
    source_file TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(metric_key, version)
);

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
);

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
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    action TEXT NOT NULL,
    entity TEXT NOT NULL,
    status TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_versions_key ON metric_versions(metric_key, version DESC);
CREATE INDEX IF NOT EXISTS idx_snapshots_key ON snapshots(metric_key, id DESC);
CREATE INDEX IF NOT EXISTS idx_snapshots_run ON snapshots(run_id);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_finished ON runs(status, finished_at);
CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_log(entity, created_at);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open a normal read/write SQLite connection."""
    path = str(db_path)
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: str | Path) -> sqlite3.Connection:
    conn = connect(db_path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    tables = ("metrics", "metric_versions", "runs", "snapshots", "audit_log")
    return {name: int(conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0])
            for name in tables}

