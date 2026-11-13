"""Tests for retention-window pruning: eligibility, dry-run, referential
integrity, batched commits, default no-op behaviour and concurrency guards."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from app.core import retention
from app.db import connect, init_db
from app.errors import CaliberError


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def _insert_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    status: str,
    started: datetime,
    finished: datetime | None,
    snapshots: int = 1,
    audit_rows: int = 1,
) -> None:
    conn.execute(
        "INSERT INTO runs(id,status,started_at,finished_at,metrics_json,"
        "datasets_dir,tolerances_json,summary_json) VALUES (?,?,?,?,?,?,?,?)",
        (
            run_id,
            status,
            _iso(started),
            _iso(finished) if finished else None,
            '["m"]',
            "/tmp/data",
            '{"abs":1e-6,"rel":0}',
            "{}",
        ),
    )
    for index in range(snapshots):
        conn.execute(
            "INSERT INTO snapshots(run_id,metric_key,metric_version,datasets_json,"
            "impl_ids_json,verdict,summary_json,result_json,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                f"m{index}",
                1,
                "[]",
                "[]",
                "consistent",
                "{}",
                "{}",
                _iso(started),
            ),
        )
    for index in range(audit_rows):
        entity = f"run:{run_id}" if index == 0 else f"run:{run_id}:extra{index}"
        conn.execute(
            "INSERT INTO audit_log(created_at,action,entity,status,detail_json) "
            "VALUES (?,?,?,?,?)",
            (_iso(started), "run.completed", entity, "ok", "{}"),
        )


def _insert_audit(conn: sqlite3.Connection, when: datetime, entity: str) -> None:
    conn.execute(
        "INSERT INTO audit_log(created_at,action,entity,status,detail_json) "
        "VALUES (?,?,?,?,?)",
        (_iso(when), "metric.import", entity, "ok", "{}"),
    )


def _counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in ("runs", "snapshots", "audit_log")
    }


def _orphan_snapshots(conn: sqlite3.Connection) -> int:
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM snapshots s LEFT JOIN runs r ON r.id = s.run_id "
            "WHERE r.id IS NULL"
        ).fetchone()[0]
    )


@pytest.fixture
def populated_db(db_path):
    """Runs spread across a 120-day timeline, plus run/non-run audit rows."""
    conn = init_db(db_path)
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=120)
    middle = now - timedelta(days=40)

    _insert_run(conn, 1, status="completed", started=old,
                finished=old + timedelta(minutes=5), snapshots=2, audit_rows=2)
    _insert_run(conn, 2, status="completed", started=old,
                finished=old + timedelta(minutes=5), snapshots=1, audit_rows=1)
    _insert_run(conn, 3, status="failed", started=old,
                finished=old + timedelta(minutes=2), snapshots=1, audit_rows=1)
    _insert_run(conn, 4, status="completed", started=middle,
                finished=middle + timedelta(minutes=5), snapshots=1, audit_rows=1)
    _insert_run(conn, 5, status="running", started=old, finished=None,
                snapshots=0, audit_rows=1)
    _insert_audit(conn, old, "metric_a")      # aged non-run audit -> prunable
    _insert_audit(conn, middle, "metric_b")   # recent non-run audit -> kept
    conn.commit()
    return conn, now


# --------------------------------------------------------------------------- #
# validation and cutoff
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("bad", [0, -1, -90, "x", 1.5, True, False, None])
def test_validate_retention_days_rejects_bad_values(bad):
    with pytest.raises(CaliberError):
        retention.validate_retention_days(bad)


@pytest.mark.parametrize("good", [1, 30, 90, "60"])
def test_validate_retention_days_accepts(good):
    assert retention.validate_retention_days(good) == int(good)


def test_cutoff_is_exactly_retention_window(monkeypatch):
    fixed = datetime(2026, 11, 13, 12, 0, 0, tzinfo=timezone.utc)
    cutoff = retention.retention_cutoff(90, now=fixed)
    assert cutoff == fixed - timedelta(days=90)


def test_batch_size_validation():
    assert retention.validate_batch_size(1) == 1
    assert retention.validate_batch_size("7") == 7
    for bad in (0, -5, "x", True):
        with pytest.raises(CaliberError):
            retention.validate_batch_size(bad)


# --------------------------------------------------------------------------- #
# dry-run: statistics match and nothing is deleted
# --------------------------------------------------------------------------- #

def test_dry_run_reports_exact_counts_and_deletes_nothing(populated_db):
    conn, _ = populated_db
    before = _counts(conn)
    plan = retention.plan_pruning(conn, 90, batch_size=2)

    assert plan.dry_run is True
    assert plan.eligible_runs == 3            # runs 1, 2, 3 (finished & old)
    assert plan.snapshots == 4                # 2 + 1 + 1
    assert plan.run_audit_records == 4        # 2 + 1 + 1
    assert plan.aged_audit_records == 1       # old metric_a only
    assert plan.audit_records == 5
    assert plan.batches == 2
    assert plan.pruned_run_ids == [1, 2, 3]
    assert _counts(conn) == before


def test_dry_run_and_actual_prune_counts_agree(populated_db):
    conn, _ = populated_db
    plan = retention.plan_pruning(conn, 90, batch_size=2)
    stats = retention.prune(conn, 90, batch_size=2)

    assert stats.dry_run is False
    assert stats.eligible_runs == plan.eligible_runs
    assert stats.snapshots == plan.snapshots
    assert stats.run_audit_records == plan.run_audit_records
    assert stats.aged_audit_records == plan.aged_audit_records
    assert stats.audit_records == plan.audit_records
    assert stats.pruned_run_ids == plan.pruned_run_ids


# --------------------------------------------------------------------------- #
# actual pruning: eligibility boundaries and referential integrity
# --------------------------------------------------------------------------- #

def test_prune_keeps_recent_running_and_failed_boundaries(populated_db):
    conn, _ = populated_db
    retention.prune(conn, 90, batch_size=2)

    remaining_runs = {row[0] for row in conn.execute("SELECT id FROM runs")}
    assert remaining_runs == {4, 5}          # recent completed + old running
    remaining_snapshot_runs = {
        row[0] for row in conn.execute("SELECT DISTINCT run_id FROM snapshots")
    }
    assert remaining_snapshot_runs == {4}
    # recent non-run audit kept; all run-scoped audit for pruned runs gone.
    # The core prune() function itself stays silent; the API/CLI boundary is
    # responsible for writing the retention.prune audit record.
    entities = {row[0] for row in conn.execute("SELECT entity FROM audit_log")}
    assert entities == {"metric_b", "run:4", "run:5"}
    assert _orphan_snapshots(conn) == 0


def test_running_run_never_pruned_even_when_ancient(db_path):
    conn = init_db(db_path)
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=400)
    _insert_run(conn, 1, status="running", started=old, finished=None,
                snapshots=0, audit_rows=1)
    conn.commit()
    stats = retention.prune(conn, 30)
    assert stats.pruned_run_ids == []
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1


def test_window_keeps_everything_when_all_runs_are_recent(db_path):
    conn = init_db(db_path)
    now = datetime.now(timezone.utc)
    _insert_run(conn, 1, status="completed", started=now - timedelta(days=2),
                finished=now - timedelta(days=2), snapshots=1, audit_rows=1)
    conn.commit()
    before = _counts(conn)
    stats = retention.prune(conn, 90)
    assert stats.pruned_run_ids == []
    assert stats.aged_audit_records == 0
    assert _counts(conn) == before


def test_finished_at_is_the_eligibility_clock(db_path):
    # A run that started long ago but finished inside the window stays.
    conn = init_db(db_path)
    now = datetime.now(timezone.utc)
    _insert_run(
        conn, 1, status="completed",
        started=now - timedelta(days=200),
        finished=now - timedelta(days=5),
    )
    conn.commit()
    plan = retention.plan_pruning(conn, 90)
    assert plan.eligible_runs == 0


# --------------------------------------------------------------------------- #
# batching
# --------------------------------------------------------------------------- #

def test_prune_commits_in_configured_batches(db_path):
    conn = init_db(db_path)
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=120)
    for rid in range(1, 7):  # 6 old runs, batch size 2 -> 3 run batches
        _insert_run(conn, rid, status="completed", started=old,
                    finished=old + timedelta(minutes=1), snapshots=1, audit_rows=1)
    conn.commit()

    seen: list[int] = []
    stats = retention.prune(
        conn, 90, batch_size=2,
        progress=lambda item: seen.append(len(item.pruned_run_ids)),
    )
    assert stats.batches == 3
    assert seen == [2, 4, 6]
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    assert _orphan_snapshots(conn) == 0


def test_batch_size_one_still_preserves_integrity(populated_db):
    conn, _ = populated_db
    stats = retention.prune(conn, 90, batch_size=1)
    assert stats.pruned_run_ids == [1, 2, 3]
    assert _orphan_snapshots(conn) == 0
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 2


# --------------------------------------------------------------------------- #
# default behaviour: no configured window means nothing is ever deleted
# --------------------------------------------------------------------------- #

def test_default_config_has_no_retention_window():
    from app.config import Config

    assert Config().retention_days is None
    assert Config().prune_batch_size == 200


def test_prune_without_window_is_rejected_by_api_contract(imported_client):
    response = imported_client.post("/api/retention/plan", json={})
    assert response.status_code == 400
    assert "保留窗口" in response.get_json()["message"]
    response = imported_client.post("/api/retention/prune", json={})
    assert response.status_code == 400


# --------------------------------------------------------------------------- #
# concurrency guards
# --------------------------------------------------------------------------- #

def test_new_run_committed_mid_prune_is_never_deleted(db_path):
    conn = init_db(db_path)
    writer = connect(db_path)
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=120)
    for rid in range(1, 11):
        _insert_run(conn, rid, status="completed", started=old,
                    finished=old + timedelta(minutes=1), snapshots=1, audit_rows=1)
    conn.commit()

    injected = {"done": False}

    def progress(stats: retention.PruneStats) -> None:
        if stats.batches == 1 and not injected["done"]:
            injected["done"] = True
            writer.execute(
                "INSERT INTO runs(status,started_at,finished_at,metrics_json,"
                "datasets_dir,tolerances_json) VALUES ('completed',?,?,?,?,?)",
                (_iso(now), _iso(now), '["m"]', "/tmp", "{}"),
            )
            new_id = writer.execute("SELECT last_insert_rowid()").fetchone()[0]
            writer.execute(
                "INSERT INTO snapshots(run_id,metric_key,metric_version,datasets_json,"
                "impl_ids_json,verdict,summary_json,result_json,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (new_id, "m", 1, "[]", "[]", "consistent", "{}", "{}", _iso(now)),
            )
            writer.execute(
                "INSERT INTO audit_log(created_at,action,entity,status,detail_json) "
                "VALUES (?,?,?,?,?)",
                (_iso(now), "run.completed", f"run:{new_id}", "ok", "{}"),
            )
            writer.commit()

    stats = retention.prune(conn, 90, batch_size=2, progress=progress)
    assert injected["done"] is True
    assert set(stats.pruned_run_ids) == set(range(1, 11))
    remaining = [row[0] for row in conn.execute("SELECT id FROM runs")]
    assert remaining == [11]
    assert _orphan_snapshots(conn) == 0
    conn.close()
    writer.close()


def test_status_flip_before_write_transaction_rolls_back_and_preserves_run(db_path):
    conn = init_db(db_path)
    writer = connect(db_path)
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=120)
    _insert_run(conn, 1, status="completed", started=old,
                finished=old, snapshots=1, audit_rows=1)
    conn.commit()

    # Flip lands after the candidate scan / read checks but before the
    # deleting write transaction opens -- the gap the guarded DELETE covers.
    original = retention._delete_run_batch
    gate = {"fired": False}

    def hooked(batch_conn, batch, cutoff, stats, max_attempts=5):
        if not gate["fired"]:
            gate["fired"] = True
            writer.execute(
                "UPDATE runs SET status='running', finished_at=NULL WHERE id=1"
            )
            writer.commit()
        return original(batch_conn, batch, cutoff, stats, max_attempts=max_attempts)

    retention._delete_run_batch = hooked
    try:
        stats = retention.prune(conn, 90, batch_size=1)
    finally:
        retention._delete_run_batch = original

    assert gate["fired"] is True
    assert stats.pruned_run_ids == []
    row = writer.execute("SELECT status, finished_at FROM runs WHERE id=1").fetchone()
    assert row[0] == "running" and row[1] is None
    # The rolled-back attempt must leave dependent data untouched.
    assert conn.execute(
        "SELECT COUNT(*) FROM snapshots WHERE run_id=1"
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE entity='run:1'"
    ).fetchone()[0] == 1
    assert _orphan_snapshots(conn) == 0
    conn.close()
    writer.close()


# --------------------------------------------------------------------------- #
# audit trail of pruning itself
# --------------------------------------------------------------------------- #

def test_core_prune_stays_silent_and_boundary_logs(populated_db):
    from app.audit import log_audit

    conn, _ = populated_db
    retention.prune(conn, 90, batch_size=2)
    # Core pruning does not emit its own audit row.
    assert conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE action='retention.prune'"
    ).fetchone()[0] == 0
    # The boundary layer (CLI/API) records the outcome explicitly.
    log_audit(conn, "retention.prune", "retention", "ok", {"runs": 3})
    row = conn.execute(
        "SELECT entity, status, detail_json FROM audit_log "
        "WHERE action='retention.prune'"
    ).fetchone()
    assert row is not None
    assert row["entity"] == "retention" and row["status"] == "ok"


def test_dry_run_writes_no_audit_record(populated_db):
    conn, _ = populated_db
    before = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    retention.plan_pruning(conn, 90)
    after = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    assert before == after


# --------------------------------------------------------------------------- #
# environment-driven configuration
# --------------------------------------------------------------------------- #

def test_config_from_env_enables_retention(monkeypatch):
    from app.config import Config

    monkeypatch.setenv("CALIBER_RETENTION_DAYS", "45")
    monkeypatch.setenv("CALIBER_PRUNE_BATCH_SIZE", "50")
    config = Config.from_env()
    assert config.retention_days == 45
    assert config.prune_batch_size == 50


def test_config_from_env_defaults_to_no_retention(monkeypatch):
    from app.config import Config

    monkeypatch.delenv("CALIBER_RETENTION_DAYS", raising=False)
    assert Config.from_env().retention_days is None


@pytest.mark.parametrize("bad", ["0", "-3", "abc"])
def test_config_from_env_rejects_bad_window(monkeypatch, bad):
    from app.config import Config

    monkeypatch.setenv("CALIBER_RETENTION_DAYS", bad)
    with pytest.raises(ValueError):
        Config.from_env()


@pytest.mark.parametrize("bad", ["0", "-1", "nope"])
def test_config_from_env_rejects_bad_batch_size(monkeypatch, bad):
    from app.config import Config

    monkeypatch.setenv("CALIBER_PRUNE_BATCH_SIZE", bad)
    with pytest.raises(ValueError):
        Config.from_env()


# --------------------------------------------------------------------------- #
# retry budget: a run that never re-qualifies surfaces a busy error
# --------------------------------------------------------------------------- #

def test_persistent_flip_conflict_raises_busy_error(db_path, monkeypatch):
    conn = init_db(db_path)
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=120)
    _insert_run(conn, 1, status="completed", started=old, finished=old,
                snapshots=1, audit_rows=1)
    conn.commit()

    # Force every guarded run DELETE to miss so the retry budget is exhausted.
    monkeypatch.setattr(
        retention, "_delete_guarded_run", lambda c, rid, cutoff: 0
    )
    with pytest.raises(CaliberError) as exc:
        retention.prune(conn, 90, batch_size=1)
    assert exc.value.code == "retention_busy"

    # Nothing was committed: the run and its dependent data all remain.
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 1

